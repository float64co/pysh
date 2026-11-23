"""The scripts in examples/: each has the right shebang, answers --help, and does its job."""
import glob, importlib.util, os, socket, subprocess, sys, tempfile, threading, unittest

from helpers import PYSH, ROOT

EX = os.path.join(ROOT, "examples")
SCRIPTS = sorted(glob.glob(os.path.join(EX, "*.sh")))


def have(*mods):
    return all(importlib.util.find_spec(m) for m in mods)


def example(name, *args, stdin=None, cwd=None, timeout=90, env=None):
    # HOME stays real: user-installed packages (~/.local) must stay importable. Caches go to a temp dir instead.
    e = dict(os.environ, XDG_CACHE_HOME=tempfile.gettempdir(), TERM="dumb", COLUMNS="120", **(env or {}))
    e.pop("NO_COLOR", None)
    cp = subprocess.run([PYSH, os.path.join(EX, name), *args], input=stdin, capture_output=True, text=True,
                        timeout=timeout, cwd=cwd, env=e)
    cp.all = cp.stdout + cp.stderr
    return cp


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=Tester", *args],
                   check=True, capture_output=True)


class Every(unittest.TestCase):
    def test_there_are_examples(self):
        self.assertGreaterEqual(len(SCRIPTS), 14)

    def test_shebang_and_executable(self):
        for p in SCRIPTS:
            with self.subTest(script=os.path.basename(p)):
                with open(p) as f: first = f.readline().rstrip("\n")
                self.assertEqual(first, "#!/usr/local/bin/pysh")
                self.assertTrue(os.access(p, os.X_OK), "not executable")

    def test_every_script_has_a_docstring(self):
        for p in SCRIPTS:
            with self.subTest(script=os.path.basename(p)):
                with open(p) as f: lines = f.read().splitlines()
                self.assertTrue(lines[1].startswith('"""'), "second line should open the docstring")

    def test_help_flag(self):
        for p in SCRIPTS:
            name = os.path.basename(p)
            with self.subTest(script=name):
                r = example(name, "--help")
                self.assertEqual(r.returncode, 0, r.all)
                self.assertIn("usage:", r.stdout)
                self.assertIn(name.removesuffix(".sh"), r.stdout.split("\n", 1)[0])

    def test_unknown_option_is_an_error_not_a_traceback(self):
        for p in SCRIPTS:
            name = os.path.basename(p)
            with self.subTest(script=name):
                r = example(name, "--no-such-option")
                self.assertEqual(r.returncode, 2, r.all)
                self.assertNotIn("Traceback", r.stderr)


@unittest.skipUnless(have("pandas", "rich"), "needs pandas and rich")
class DiskHog(unittest.TestCase):
    def test_reports_then_compares_with_the_last_run(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "big")); os.makedirs(os.path.join(d, "small"))
            with open(os.path.join(d, "big", "f"), "wb") as f: f.write(b"x" * 300_000)
            with open(os.path.join(d, "small", "f"), "wb") as f: f.write(b"x" * 1000)
            db = os.path.join(d, "h.sqlite")
            first = example("diskhog.sh", d, "--db", db)
            self.assertEqual(first.returncode, 0, first.all)
            self.assertLess(first.stdout.index("big"), first.stdout.index("small"))
            self.assertNotIn("since", first.stdout)
            with open(os.path.join(d, "big", "g"), "wb") as f: f.write(b"x" * 200_000)
            second = example("diskhog.sh", d, "--db", db)
            self.assertIn("since", second.stdout)
            self.assertIn("+", second.stdout)

    def test_markup_in_a_directory_name_is_shown_literally(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "[bold red]x[red]"))
            r = example("diskhog.sh", d, "--no-history")
            self.assertIn("[bold red]x[red]", r.stdout)

    def test_not_a_directory(self):
        r = example("diskhog.sh", "/no/such/dir")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a directory", r.stderr)


@unittest.skipUnless(have("psutil", "rich"), "needs psutil and rich")
class ProcWatch(unittest.TestCase):
    def test_runs(self):
        r = example("procwatch.sh", "-i", "0.2", "-n", "3")
        self.assertIn(r.returncode, (0, 1), r.all)
        self.assertIn("per user", r.stdout)
        self.assertIn("command", r.stdout)

    def test_threshold_zero_always_warns(self):
        r = example("procwatch.sh", "-i", "0.2", "--warn-cpu", "0")
        self.assertEqual(r.returncode, 1, r.all)


@unittest.skipUnless(have("pandas", "rich"), "needs pandas and rich")
class Listeners(unittest.TestCase):
    def test_sees_a_listening_socket(self):
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
        try:
            port = s.getsockname()[1]
            r = example("listeners.sh", "--csv", "--proto", "tcp")
            self.assertEqual(r.returncode, 0, r.all)
            self.assertTrue(r.stdout.startswith("proto,address,port,exposure,process,pid"))
            rows = [l.split(",") for l in r.stdout.splitlines()[1:]]
            mine = [c for c in rows if c[2] == str(port)]
            self.assertTrue(mine, f"port {port} not found")
            self.assertEqual(mine[0][3], "ALL")
            self.assertEqual(example("listeners.sh", "--exposed", "--fail-exposed").returncode, 1)
        finally:
            s.close()


@unittest.skipUnless(have("cryptography", "rich"), "needs cryptography and rich")
class CertCheck(unittest.TestCase):
    def test_demo(self):
        r = example("certcheck.sh", "--demo")
        self.assertEqual(r.returncode, 1, r.all)           # the 10-day certificate is inside the 14-day window
        self.assertIn("expires soon", r.stdout)
        r = example("certcheck.sh", "--demo", "--warn", "5")
        self.assertEqual(r.returncode, 0, r.all)

    def test_no_hosts_is_a_usage_error(self):
        self.assertEqual(example("certcheck.sh").returncode, 2)

    def test_unreachable_host_fails(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = example("certcheck.sh", f"127.0.0.1:{port}")
        self.assertEqual(r.returncode, 1, r.all)
        self.assertNotIn("Traceback", r.all)


@unittest.skipUnless(have("pandas", "rich"), "needs pandas and rich")
class GitStats(unittest.TestCase):
    def test_small_repo(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            for i in range(3):
                with open(os.path.join(d, "f.txt"), "a") as f: f.write(f"line {i}\n")
                git(d, "add", "."); git(d, "commit", "-qm", f"change {i}")
            r = example("gitstats.sh", d)
            self.assertEqual(r.returncode, 0, r.all)
            self.assertIn("3 commits", r.stdout)
            self.assertIn("Tester", r.stdout)
            self.assertIn("f.txt", r.stdout)

    def test_not_a_repository_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            r = example("gitstats.sh", d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("not a git repository", r.stderr)

    def test_hostile_author_name(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            with open(os.path.join(d, "f"), "w") as f: f.write("x\n")
            subprocess.run(["git", "-C", d, "add", "."], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=[red]Eve[/red]\x1b]0;pwned\x07", "commit", "-qm", "x"],
                           check=True, capture_output=True)
            r = example("gitstats.sh", d)
            self.assertNotIn("\x1b]0;pwned", r.stdout)
            self.assertIn("[red]Eve[/red]", r.stdout)


@unittest.skipUnless(have("pandas", "numpy", "scipy", "rich"), "needs pandas, numpy, scipy and rich")
class CsvProfile(unittest.TestCase):
    def test_demo(self):
        r = example("csvprofile.sh")
        self.assertEqual(r.returncode, 0, r.all)
        for word in ("rows", "numeric columns", "income", "plan"): self.assertIn(word, r.stdout)

    def test_file_and_stdin(self):
        data = "a,b,name\n1,2,x\n2,4,y\n3,6,x\n4,8,y\n5,10.5,x\n"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f: f.write(data)
        try:
            r = example("csvprofile.sh", f.name)
            self.assertEqual(r.returncode, 0, r.all)
            self.assertIn("5 rows", r.stdout)
            self.assertIn("correlated pairs", r.stdout)             # a and b are almost perfectly correlated
            self.assertEqual(example("csvprofile.sh", "-", stdin=data).stdout.count("5 rows"), 1)
        finally:
            os.unlink(f.name)

    def test_missing_file_and_markup_in_headers(self):
        r = example("csvprofile.sh", "/no/such.csv")
        self.assertEqual(r.returncode, 1); self.assertIn("csvprofile:", r.stderr)
        r = example("csvprofile.sh", "-", stdin="[bold]x[/bold],y\n1,2\n3,4\n")
        self.assertIn("[bold]x[/bold]", r.stdout)


@unittest.skipUnless(have("numpy", "scipy"), "needs numpy and scipy")
class AbTest(unittest.TestCase):
    def test_demo_finds_the_difference(self):
        r = example("abtest.sh")
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("Welch", r.stdout)
        self.assertIn("a difference is supported", r.stdout)

    def test_identical_samples_find_nothing(self):
        r = example("abtest.sh", "--a", "1,2,3,4,5,6,7,8,9", "--b", "1,2,3,4,5,6,7,8,9")
        self.assertEqual(r.returncode, 1, r.all)
        self.assertIn("no reliable difference", r.stdout)
        self.assertIn("small sample", r.stdout)

    def test_binary_outcomes_use_a_proportions_test(self):
        a = ",".join(["1"] * 10 + ["0"] * 90); b = ",".join(["1"] * 30 + ["0"] * 70)
        r = example("abtest.sh", "--a", a, "--b", b)
        self.assertIn("chi-squared", r.stdout)
        self.assertEqual(r.returncode, 0, r.all)

    def test_skewed_data_falls_back_to_mann_whitney(self):
        a = ",".join(str(x) for x in [1] * 30 + [2] * 5 + [100]); b = ",".join(str(x) for x in [5] * 30 + [6] * 5 + [300])
        self.assertIn("Mann-Whitney", example("abtest.sh", "--a", a, "--b", b).stdout)

    def test_csv_with_groups_and_bad_arguments(self):
        rows = "g,v\n" + "\n".join(f"a,{10 + i % 5}" for i in range(40)) + "\n" + "\n".join(f"b,{20 + i % 5}" for i in range(40)) + "\n"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f: f.write(rows)
        try:
            r = example("abtest.sh", "--csv", f.name, "--group", "g", "--value", "v")
            self.assertEqual(r.returncode, 0, r.all)
            self.assertEqual(example("abtest.sh", "--a", "1,2,3").returncode, 2)
            self.assertEqual(example("abtest.sh", "--a", "1", "--b", "2").returncode, 1)
        finally:
            os.unlink(f.name)


class Flaky(unittest.TestCase):
    def test_stable_command(self):
        r = example("flaky.sh", "-n", "5", "--", "true")
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("stable", r.stdout)

    def test_always_failing_command_and_its_message(self):
        r = example("flaky.sh", "-n", "4", "--", "sh", "-c", "echo 'boom 17' >&2; exit 3")
        self.assertEqual(r.returncode, 1, r.all)
        self.assertIn("BROKEN", r.stdout)
        self.assertIn("boom N", r.stdout)                       # digits masked
        self.assertIn("3×4", r.stdout)

    def test_sometimes_failing_command_is_flaky(self):
        with tempfile.TemporaryDirectory() as d:
            counter = os.path.join(d, "n")
            script = f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; [ $((n % 2)) -eq 0 ]"
            r = example("flaky.sh", "-n", "10", "-j", "1", "--", "sh", "-c", script)
            self.assertIn("FLAKY", r.stdout, r.all)

    def test_timeout(self):
        r = example("flaky.sh", "-n", "2", "-t", "0.5", "--", "sleep", "5")
        self.assertIn("timeout×2", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_missing_command(self):
        r = example("flaky.sh", "-n", "2", "--", "no-such-command-xyz")
        self.assertIn("command not found", r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_demo_runs(self):
        r = example("flaky.sh", "-n", "10")
        self.assertIn("passed", r.stdout)


@unittest.skipUnless(have("numpy", "rich"), "needs numpy and rich")
class HashSweep(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.d])
        os.makedirs(os.path.join(self.d, "sub"))
        def w(name, data):
            with open(os.path.join(self.d, name), "wb") as f: f.write(data)
        w("a.txt", b"hello\n"); w("sub/b.txt", b"hello\n")
        w("blob.dat", os.urandom(20000)); w("pack.zip", os.urandom(20000)); w("bad.bin", b"evil\n")

    def test_duplicates_entropy_and_ioc(self):
        import hashlib
        ioc = os.path.join(tempfile.mkdtemp(), "iocs.txt")           # outside the swept directory
        self.addCleanup(subprocess.run, ["rm", "-rf", os.path.dirname(ioc)])
        with open(ioc, "w") as f: f.write("# known bad\n" + hashlib.sha256(b"evil\n").hexdigest() + "  trojan\n")
        r = example("hashsweep.sh", self.d, "--ioc", ioc)
        self.assertEqual(r.returncode, 1, r.all)
        self.assertIn("IOC MATCHES", r.stdout)
        self.assertIn("duplicates: 1 sets", r.stdout)
        self.assertIn("blob.dat", r.stdout)
        self.assertNotIn("pack.zip", r.stdout)               # high entropy, but an expected compressed format
        self.assertIn("5 files", r.stdout)

    def test_clean_directory_exits_zero(self):
        r = example("hashsweep.sh", self.d)
        self.assertEqual(r.returncode, 0, r.all)

    def test_bad_arguments(self):
        self.assertEqual(example("hashsweep.sh", "/no/such/dir").returncode, 1)
        self.assertEqual(example("hashsweep.sh", self.d, "--ioc", "/no/such").returncode, 1)


@unittest.skipUnless(have("pandas", "rich"), "needs pandas and rich")
class AuthLog(unittest.TestCase):
    def test_sample(self):
        r = example("authlog.sh", "--sample")
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("203.0.113.9", r.stdout)
        self.assertIn("sudo ufw deny from 203.0.113.9", r.stdout)
        self.assertIn("nothing was changed", r.stdout)

    def test_old_syslog_format_and_hostile_content(self):
        log = ("Mar  2 03:14:01 h sshd[1]: Failed password for [red]root[/red]\x1b]0;pwned\x07 from 203.0.113.9 port 1 ssh2\n"
               "Mar  2 03:14:02 h sshd[1]: Failed password for root from 203.0.113.9;rm -rf / port 1 ssh2\n"      # not a valid address
               "Mar  2 03:14:03 h sshd[1]: Failed password for root from 203.0.113.9 port 2 ssh2\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as f: f.write(log)
        try:
            r = example("authlog.sh", f.name, "--min", "2", "--year", "2025")
            self.assertEqual(r.returncode, 0, r.all)
            self.assertNotIn("\x1b]0;pwned", r.stdout)
            self.assertNotIn("rm -rf", r.stdout)             # a malformed address never reaches the suggested command
            self.assertIn("sudo ufw deny from 203.0.113.9\n", r.stdout)
        finally:
            os.unlink(f.name)

    def test_no_failures_and_missing_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f: f.write("nothing interesting\n")
        try: self.assertIn("no failed SSH logins", example("authlog.sh", f.name).stdout)
        finally: os.unlink(f.name)
        self.assertEqual(example("authlog.sh", "/no/such/log").returncode, 1)


@unittest.skipUnless(have("networkx", "rich"), "needs networkx and rich")
class DepGraph(unittest.TestCase):
    def make(self, files):
        d = tempfile.mkdtemp(); self.addCleanup(subprocess.run, ["rm", "-rf", d])
        for name, text in files.items():
            p = os.path.join(d, name); os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f: f.write(text)
        return d

    def test_cycle_and_ranking(self):
        d = self.make({"pkg/__init__.py": "", "pkg/a.py": "from . import b\nimport requests\n", "pkg/b.py": "from pkg import a\n",
                       "pkg/c.py": "from .a import x\nimport os\n"})
        r = example("depgraph.sh", d)
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("1 import cycle", r.stdout)
        self.assertIn("pkg.a", r.stdout)
        self.assertIn("requests", r.stdout)
        self.assertEqual(example("depgraph.sh", d, "--fail-on-cycles").returncode, 1)

    def test_lazy_imports_are_ignored_unless_asked(self):
        d = self.make({"a.py": "import b\n", "b.py": "def f():\n    import a\n"})
        self.assertIn("no import cycles", example("depgraph.sh", d).stdout)
        self.assertIn("1 import cycle", example("depgraph.sh", d, "--include-lazy").stdout)

    def test_dot_output_and_broken_files(self):
        d = self.make({"a.py": "import b\n", "b.py": "x = 1\n", "broken.py": "def (:\n"})
        dot = os.path.join(d, "g.dot")
        r = example("depgraph.sh", d, "--dot", dot)
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("could not be parsed", r.stdout)
        with open(dot) as f: self.assertIn('"a" -> "b";', f.read())

    def test_empty_and_missing(self):
        self.assertIn("no Python files", example("depgraph.sh", self.make({"x.txt": ""})).stdout)
        self.assertEqual(example("depgraph.sh", "/no/such").returncode, 1)


class LlmCommit(unittest.TestCase):
    def repo(self):
        d = tempfile.mkdtemp(); self.addCleanup(subprocess.run, ["rm", "-rf", d])
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, "app.py"), "w") as f: f.write("print('hi')\n")
        git(d, "add", "."); git(d, "commit", "-qm", "Add app")
        return d

    def test_prompt_contains_diff_and_history(self):
        d = self.repo()
        with open(os.path.join(d, "app.py"), "a") as f: f.write("def greet(): pass\n")
        git(d, "add", "app.py")
        r = example("llm-commit.sh", "--print-prompt", cwd=d)
        self.assertEqual(r.returncode, 0, r.all)
        self.assertIn("+def greet(): pass", r.stdout)
        self.assertIn("Add app", r.stdout)
        self.assertIn("Conventional Commits", example("llm-commit.sh", "--print-prompt", "--style", "conventional", cwd=d).stdout)

    def test_long_diffs_are_truncated(self):
        d = self.repo()
        with open(os.path.join(d, "big.txt"), "w") as f: f.write("line\n" * 5000)
        git(d, "add", "big.txt")
        self.assertIn("diff truncated", example("llm-commit.sh", "--print-prompt", "--max-chars", "500", cwd=d).stdout)

    def test_nothing_staged_and_not_a_repo(self):
        r = example("llm-commit.sh", "--print-prompt", cwd=self.repo())
        self.assertEqual(r.returncode, 1); self.assertIn("nothing is staged", r.stderr)
        with tempfile.TemporaryDirectory() as d:
            r = example("llm-commit.sh", "--print-prompt", cwd=d)
            self.assertEqual(r.returncode, 1); self.assertIn("not inside a git repository", r.stderr)

    def test_unreachable_server_is_a_clear_error(self):
        d = self.repo()
        with open(os.path.join(d, "app.py"), "a") as f: f.write("x = 1\n")
        git(d, "add", "app.py")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = example("llm-commit.sh", "--host", f"127.0.0.1:{port}", cwd=d)
        self.assertEqual(r.returncode, 1, r.all)
        self.assertNotIn("Traceback", r.stderr)


@unittest.skipUnless(have("jinja2", "lxml", "rich"), "needs jinja2, lxml and rich")
class PortScan(unittest.TestCase):
    def test_finds_an_open_port_and_its_banner(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5); port = srv.getsockname()[1]
        def serve():
            while True:
                try: c, _ = srv.accept()
                except OSError: return
                c.sendall(b"SSH-2.0-TestServer_1.0 \x1b]0;pwned\x07\r\n"); c.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                report = os.path.join(d, "r.md")
                r = example("portscan.sh", "-p", f"{port},{port + 1}", "--no-nmap", "--report", report)
                self.assertEqual(r.returncode, 0, r.all)
                self.assertIn(str(port), r.stdout)
                self.assertIn("SSH-2.0-TestServer_1.0", r.stdout)
                self.assertNotIn("\x1b]0;pwned", r.stdout)
                with open(report) as f: text = f.read()
                self.assertIn(f"| {port} |", text)
                self.assertNotIn(f"| {port + 1} |", text)
        finally:
            srv.close()

    def test_refuses_public_addresses_without_permission(self):
        r = example("portscan.sh", "8.8.8.8", "-p", "53")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a loopback or private address", r.stderr)

    def test_bad_port_spec(self):
        self.assertEqual(example("portscan.sh", "-p", "99999").returncode, 2)
        self.assertEqual(example("portscan.sh", "-p", "abc").returncode, 2)

    def test_unresolvable_host(self):
        r = example("portscan.sh", "no-such-host.invalid")
        self.assertEqual(r.returncode, 1); self.assertIn("cannot resolve", r.stderr)


@unittest.skipUnless(have("pwn"), "needs pwntools")
class RopNotes(unittest.TestCase):
    def test_tour_of_a_system_binary(self):
        r = example("ropnotes.sh", "--find", "0x6161616b", timeout=120)
        self.assertEqual(r.returncode, 0, r.all)
        for word in ("checksec", "0x6161616b is at offset 40", "48c7c03c0000004831ff0f05", "mov    rax, 0x3c"): self.assertIn(word, r.stdout)

    def test_not_an_elf_and_missing_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f: f.write("just text\n")
        try:
            r = example("ropnotes.sh", f.name, timeout=120)
            self.assertEqual(r.returncode, 1, r.all); self.assertIn("not a readable ELF", r.stderr)
        finally:
            os.unlink(f.name)
        self.assertEqual(example("ropnotes.sh", "/no/such/file").returncode, 1)

    def test_bad_assembly_is_reported(self):
        r = example("ropnotes.sh", "--asm", "nonsense instruction", timeout=120)
        self.assertEqual(r.returncode, 1); self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()

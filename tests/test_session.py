"""Virtualenvs, the login/interactive options other programs pass to a shell, and --norc."""
import os, shutil, subprocess, sys, tempfile, unittest

from helpers import PYSH
from term import Session

VER = f"{sys.version_info[0]}.{sys.version_info[1]}"


def make_venv(parent, name=".venv"):
    root = os.path.join(parent, name)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", root], check=True)
    ver = os.listdir(os.path.join(root, "lib"))[0]
    with open(os.path.join(root, "lib", ver, "site-packages", "onlyvenv.py"), "w") as f: f.write("WHO = 'venv'\n")
    return root


class Home(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="pysh-test-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}
        return dict(e, HOME=self.d, **extra)

    def run_pysh(self, *args, stdin="", env=None, argv0=None):
        kw = {"executable": PYSH} if argv0 else {}
        return subprocess.run([argv0 or PYSH, *args], input=stdin, capture_output=True, text=True, timeout=60,
                              cwd=self.d, env=env or self.env(), **kw)

    def rc(self, text):
        with open(os.path.join(self.d, ".pyshrc"), "w") as f: f.write(text)


class Virtualenv(Home):
    def script(self, text, **kw):
        r = self.run_pysh(stdin=text, **kw)
        return r.stdout, r.stderr

    def test_activate_changes_commands_and_imports(self):
        root = make_venv(self.d)
        out, err = self.script("activate\nimport onlyvenv, sys\nprint(onlyvenv.WHO, sys.prefix, sys.executable)\n"
                               "python -c 'import sys; print(sys.prefix)'\necho $VIRTUAL_ENV\n"
                               "print([p for p in sys.path if 'packages' in p])\n")
        lines = out.splitlines()
        self.assertEqual(lines[0], f"venv {root} {root}/bin/python", err)
        self.assertEqual(lines[1:3], [root, root])
        self.assertEqual(lines[3], str([f"{root}/lib/python{VER}/site-packages"]))        # isolated from the system's packages

    def test_deactivate_restores_everything(self):
        make_venv(self.d)
        out, err = self.script("import sys, os\nbefore = (list(sys.path), sys.prefix, sys.executable, os.environ['PATH'])\n"
                               "activate .venv\nimport onlyvenv\ndeactivate\n"
                               "print((list(sys.path), sys.prefix, sys.executable, os.environ['PATH']) == before)\n"
                               "print('VIRTUAL_ENV' in os.environ, 'onlyvenv' in sys.modules)\nimport onlyvenv\n")
        self.assertEqual(out.split(), ["True", "False", "False"], err)
        self.assertIn("No module named 'onlyvenv'", err)

    def test_source_bin_activate_works(self):
        root = make_venv(self.d, "env")
        for command in ("source env/bin/activate", f". {root}/bin/activate"):
            with self.subTest(command=command):
                out, err = self.script(command + "\necho $VIRTUAL_ENV\necho still-works\n")
                self.assertEqual(out.split(), [root, "still-works"], err)
                self.assertEqual(err, "")

    def test_system_site_packages_venv_keeps_them(self):
        root = make_venv(self.d)
        cfg = os.path.join(root, "pyvenv.cfg")
        with open(cfg) as f: text = f.read()
        with open(cfg, "w") as f: f.write(text.replace("include-system-site-packages = false", "include-system-site-packages = true"))
        out, err = self.script("import sys\nn = [p for p in sys.path if 'dist-packages' in p]\nactivate\n"
                               "print(all(p in sys.path for p in n))\nimport onlyvenv\nprint(onlyvenv.WHO)\n")
        self.assertEqual(out.split(), ["True", "venv"], err)

    def test_other_python_version_gets_path_only_and_says_so(self):
        root = make_venv(self.d)
        with open(os.path.join(root, "pyvenv.cfg"), "w") as f: f.write("home = /usr/bin\nversion = 2.7.18\n")
        out, err = self.script("import sys\nbefore = list(sys.path)\nactivate\nprint(sys.path == before)\necho $VIRTUAL_ENV\n")
        self.assertEqual(out.split(), ["True", root])
        self.assertIn("2.7.18 virtualenv", err)

    def test_errors(self):
        out, err = self.script("activate\nactivate /nonexistent\ndeactivate\necho alive\n")
        self.assertEqual(out.split(), ["alive"])
        for words in ("no .venv or venv here", "not a virtualenv", "no virtualenv is active"): self.assertIn(words, err)

    def test_switching_between_two(self):
        a, b = make_venv(self.d, "a"), make_venv(self.d, "b")
        out, err = self.script("import os\nactivate a\nactivate b\necho $VIRTUAL_ENV\n"
                               f"print('{a}/bin' in os.environ['PATH'], '{b}/bin' in os.environ['PATH'])\n")
        self.assertEqual(out.split(), [b, "False", "True"], err)

    def test_started_inside_an_activated_venv(self):
        root = make_venv(self.d)
        env = self.env(VIRTUAL_ENV=root, PATH=f"{root}/bin:{os.environ['PATH']}")
        r = self.run_pysh("-c", "import onlyvenv\nprint(onlyvenv.WHO)\ndeactivate\nprint('done')\n", env=env)
        self.assertEqual(r.stdout.split(), ["venv", "done"], r.stderr)

    def test_prompt_shows_the_active_venv(self):
        make_venv(self.d)
        s = Session(rows=30, cols=90, home=self.d, env={"VIRTUAL_ENV": ""}); self.addCleanup(s.close)
        s.send("activate\r", 0.8)
        self.assertIn("(.venv) tester@", s.text())
        s.send("deactivate\r", 0.8)
        self.assertTrue(s.text().rstrip().split("\n")[-1].startswith("tester@"), s.text())


class Options(Home):
    """Programs start shells with -l, -i, -c, combined (`-lic`) or as argv[0] "-pysh"."""
    def setUp(self):
        super().setUp()
        self.rc("export FROM_RC=1\nalias hi='echo HI'\n")

    def out(self, *args, **kw):
        r = self.run_pysh(*args, **kw)
        self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
        return r.stdout.strip()

    def test_login_flags_are_accepted_and_do_not_read_the_rc(self):
        for args in (("-l", "-c"), ("--login", "-c"), ("-lc",), ("-c", "-l")):
            with self.subTest(args=args): self.assertEqual(self.out(*args, "echo [$FROM_RC]"), "[]")
        self.assertEqual(self.out("-l", stdin="echo piped\n"), "piped")

    def test_login_by_argv0(self):
        self.assertEqual(self.out("-c", "echo ok", argv0="-pysh"), "ok")

    def test_interactive_reads_the_rc_first(self):
        for args in (("-ic",), ("-lic",), ("-i", "-l", "-c"), ("--interactive", "-c")):
            with self.subTest(args=args): self.assertEqual(self.out(*args, "echo [$FROM_RC]"), "[1]")
        self.assertEqual(self.out("-ic", "hi"), "HI")
        self.assertEqual(self.out("-i", stdin="echo [$FROM_RC]\n"), "[1]")
        script = os.path.join(self.d, "s.pysh")
        with open(script, "w") as f: f.write("echo [$FROM_RC]\n")
        self.assertEqual(self.out("-i", script), "[1]")
        self.assertEqual(self.out(script), "[]")

    def test_interactive_command_never_creates_the_rc(self):
        os.remove(os.path.join(self.d, ".pyshrc"))
        self.assertEqual(self.out("-ic", "echo hi"), "hi")
        self.assertFalse(os.path.exists(os.path.join(self.d, ".pyshrc")))

    def test_norc(self):
        self.assertEqual(self.out("--norc", "-ic", "echo [$FROM_RC]"), "[]")

    def test_unknown_options_are_still_errors(self):
        for bad in ("-x", "--bogus", "-lz"):
            with self.subTest(bad=bad):
                r = self.run_pysh(bad)
                self.assertEqual(r.returncode, 2); self.assertIn("unknown option", r.stderr)


class NoRc(Home):
    def test_gets_you_past_an_rc_that_exits(self):
        self.rc("exit 3\n")
        s = Session(args=("--norc",), rows=30, cols=80, home=self.d); self.addCleanup(s.close)
        s.send("echo got-in\r", 0.8)
        self.assertIn("\ngot-in", s.text())

    def test_does_not_create_one(self):
        s = Session(args=("--norc",), rows=30, cols=80, home=self.d); self.addCleanup(s.close)
        s.send("echo hi\r", 0.6)
        self.assertFalse(os.path.exists(os.path.join(self.d, ".pyshrc")))


if __name__ == "__main__":
    unittest.main()

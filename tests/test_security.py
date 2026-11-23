"""Regression tests for the security review: untrusted programs/expressions must not run by accident,
hostile text must not reach the terminal as escape sequences, secrets stay private, and nothing hangs."""
import os, stat, subprocess, tempfile, time, unittest
from helpers import Base, run, out, PYSH
from term import Session

ESC = "\x1b"


def script(path, body):
    with open(path, "w") as f: f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


def pysh_py(code, cwd=None, env=None):
    """Run Python in pysh's own module namespace (for the internal helpers: shell.clean, shell.complete ...)."""
    e = dict(os.environ, **(env or {}))
    r = subprocess.run([PYSH, "-c", code], capture_output=True, text=True, timeout=60, cwd=cwd, env=e)
    return r.stdout.strip(), r.stderr.strip()


class FlagProbing(unittest.TestCase):
    def test_system_programs_are_probed_by_default(self):
        o, _ = pysh_py("print(len(shell.flags_for('ls')) > 5)")
        self.assertEqual(o, "True")

    def test_trusted_command_is_a_system_path(self):
        o, _ = pysh_py("print(shell.trusted_command('ls'))")
        self.assertTrue(o.startswith("/") and o.endswith("/ls"), o)

    def test_relative_paths_are_never_trusted(self):
        o, _ = pysh_py("print(shell.trusted_command('./ls'), shell.trusted_command('../x/ls'), shell.trusted_command('/bin/ls'))")
        self.assertEqual(o, "None None None")

    def test_user_owned_directory_on_path_is_not_trusted(self):
        d = tempfile.mkdtemp(); script(os.path.join(d, "mytool"), "echo hi\n")
        o, _ = pysh_py("print(shell.trusted_command('mytool'))", env={"PATH": d + ":/usr/bin:/bin"})
        self.assertEqual(o, "None")

    def test_untrusted_earlier_directory_shadows_the_system_program(self):
        d = tempfile.mkdtemp(); script(os.path.join(d, "ls"), "echo shadow\n")
        o, _ = pysh_py("print(shell.trusted_command('ls'))", env={"PATH": d + ":/usr/bin:/bin"})
        self.assertEqual(o, "None")                               # the shadowing program is what would run

    def test_dot_and_empty_path_entries_are_not_trusted(self):
        d = tempfile.mkdtemp(); script(os.path.join(d, "ls"), "echo shadow\n")
        for path in (".:/usr/bin:/bin", ":/usr/bin:/bin"):
            o, _ = pysh_py("print(shell.trusted_command('ls'))", cwd=d, env={"PATH": path})
            self.assertEqual(o, "None", path)

    def test_group_writable_system_file_is_not_trusted(self):
        o, _ = pysh_py("import os, tempfile, stat\nd = tempfile.mkdtemp()\np = d + '/tool'\nopen(p, 'w').write('#!/bin/sh\\n')\n"
                       "os.chmod(p, 0o755)\nprint(shell._root_safe(p), shell._root_safe('/usr/bin/ls'))")
        self.assertEqual(o, "False True")                         # a file we own is not root-owned

    def test_probing_can_be_switched_off(self):
        o, _ = pysh_py("shell.ns['probe_flags'] = False\nprint(shell.flags_for('ls'))")
        self.assertEqual(o, "[]")

    def test_tab_does_not_run_a_program_from_the_current_directory(self):
        s = Session(); self.addCleanup(s.close)
        marker = os.path.join(s.home, "RAN")
        script(os.path.join(s.home, "evil"), f"touch {marker}\necho '  -x  an option'\n")
        s.send("./evil -\t", 1.5)
        s.send("evil -\t", 1.5)
        self.assertFalse(os.path.exists(marker))

    def test_tab_does_not_run_an_untrusted_program_found_through_path(self):
        d = tempfile.mkdtemp(); marker = os.path.join(d, "RAN")
        script(os.path.join(d, "evil"), f"touch {marker}\necho '  -x  an option'\n")
        s = Session(env={"PATH": d + ":/usr/bin:/bin"}); self.addCleanup(s.close)
        s.send("evil -\t", 1.5)
        self.assertFalse(os.path.exists(marker))

    def test_flag_completion_still_works_for_system_programs(self):
        s = Session(rows=24, cols=80); self.addCleanup(s.close)
        s.send("ls --almost\t", 1.5)
        self.assertIn("--almost-all", s.text())


class AttributeCompletion(unittest.TestCase):
    def test_typed_expressions_are_not_evaluated(self):
        d = tempfile.mkdtemp()
        pysh_py("shell.complete(\"__import__('os').mkdir('RAN1').x\")\nshell.complete('print(open(chr(82)+chr(65)+chr(78)+chr(50),chr(119))).x')", cwd=d)
        self.assertEqual(os.listdir(d), [])

    def test_calls_and_indexing_are_rejected(self):
        o, _ = pysh_py("print(shell.complete('len([1]).re')[1], shell.complete('x[0].re')[1])")
        self.assertEqual(o, "[] []")

    def test_properties_are_not_run(self):
        d = tempfile.mkdtemp()
        pysh_py("class A:\n    @property\n    def boom(self):\n        open('RAN3', 'w').close()\n        return 1\n"
                "a = A()\nshell.ns['a'] = a\nshell.complete('a.boom.re')", cwd=d)
        self.assertEqual(os.listdir(d), [])

    def test_ordinary_completion_still_works(self):
        o, _ = pysh_py("import os\nshell.ns['os'] = os\nprint([d for _, d, _ in shell.complete('os.path.jo')[1]])")
        self.assertEqual(o, "['join']")

    def test_dynamic_attributes_still_complete(self):
        o, _ = pysh_py("class D:\n    def __getattr__(self, n):\n        if n == 'inner': return type('I', (), {'leaf': 1})()\n        raise AttributeError(n)\n"
                       "shell.ns['d'] = D()\nprint([x for _, x, _ in shell.complete('d.inner.le')[1]])")
        self.assertEqual(o, "['leaf']")

    def test_builtins_complete(self):
        o, _ = pysh_py("print(bool(shell.complete('str.jo')[1]))")
        self.assertEqual(o, "True")


class TerminalEscapes(unittest.TestCase):
    OSC = "\x1b]0;PWNED\x07"

    def test_clean_helper(self):
        o, _ = pysh_py("print(repr(shell.clean('a\\x1b]0;t\\x07b\\x9bc\\td\\ne')))")
        self.assertEqual(o, "'a?]0;t?b?c\\td\\ne'")              # tab and newline are left alone

    def test_directory_name_cannot_inject_escapes_into_the_prompt(self):
        base = tempfile.mkdtemp(); d = os.path.join(base, "dir" + self.OSC + "x"); os.mkdir(d)
        o, _ = pysh_py("print(repr(shell.prompt()))", cwd=d)
        self.assertNotIn("\\x07", o); self.assertNotIn("]0;PWNED\\x07", o)
        self.assertIn("?]0;PWNED?x", o)

    def test_git_branch_cannot_inject_escapes(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f: f.write("ref: refs/heads/a" + self.OSC + "b\n")
        o, _ = pysh_py("print(repr(shell.git_branch()), repr(shell.prompt()))", cwd=d)
        self.assertNotIn("\\x07", o)
        self.assertIn("a?]0;PWNED?b", o)

    def test_detached_head_cannot_inject_escapes(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f: f.write("\x1b[2J\x1b]52;c;xx\x07\n")
        o, _ = pysh_py("print(repr(shell.git_branch()))", cwd=d)
        self.assertNotIn("\\x1b", o)

    def test_error_messages_cannot_inject_escapes(self):
        r = run("cat < '/nonexistent/" + self.OSC + "file'\necho done")
        self.assertNotIn("\x07", r.err); self.assertNotIn("\x1b", r.err)
        self.assertIn("?]0;PWNED?", r.err)

    def test_history_suggestion_cannot_inject_escapes(self):
        o, _ = pysh_py("shell.history.append('echo a" + "\\x1b]0;PWNED\\x07" + "b')\nprint(repr(shell.suggest('echo a')))")
        self.assertNotIn("\\x1b", o); self.assertNotIn("\\x07", o)

    def test_highlighting_cannot_inject_escapes(self):
        o, _ = pysh_py("h = shell.highlight('echo a\\x1b]0;PWNED\\x07b')\nprint(repr(h))")
        self.assertNotIn("]0;PWNED\\x07", o)
        o, _ = pysh_py("print(len(shell.highlight('echo \\x1bX')) > 0, '\\x1bX' in shell.highlight('echo \\x1bX'))")
        self.assertEqual(o, "True False")

    def test_prompt_on_a_real_terminal_shows_no_title_change(self):
        base = tempfile.mkdtemp(); d = os.path.join(base, "d" + self.OSC + "x"); os.mkdir(d)
        s = Session(); self.addCleanup(s.close)
        s.send("cd " + d.replace("\x1b", "\\\x1b").replace("\x07", "\\\x07") + "\r", 0.8)
        self.assertNotIn(self.OSC.encode(), s.raw)


class HistoryFile(unittest.TestCase):
    def mode(self, s): return stat.S_IMODE(os.stat(os.path.join(s.home, ".pysh_history")).st_mode)

    def test_created_private(self):
        old = os.umask(0o022)
        try:
            s = Session(); self.addCleanup(s.close)
            s.send("echo secret-token\r", 0.6)
        finally: os.umask(old)
        self.assertEqual(self.mode(s), 0o600)

    def test_existing_world_readable_file_is_tightened(self):
        home = tempfile.mkdtemp(); p = os.path.join(home, ".pysh_history")
        with open(p, "w") as f: f.write("echo old\n")
        os.chmod(p, 0o644)
        s = Session(home=home); self.addCleanup(s.close)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_leading_space_keeps_a_command_out_of_history(self):
        s = Session(); self.addCleanup(s.close)
        s.send(" echo hidden-secret\r", 0.6); s.send("echo visible\r", 0.6)
        with open(os.path.join(s.home, ".pysh_history")) as f: text = f.read()
        self.assertIn("echo visible", text); self.assertNotIn("hidden-secret", text)

    def test_ttylog_is_private(self):
        log = os.path.join(tempfile.mkdtemp(), "log")
        s = Session(env={"PYSH_TTYLOG": log}); self.addCleanup(s.close)
        s.send("echo hi\r", 0.4)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)


class RelativePath(unittest.TestCase):
    def test_startup_does_not_run_ls_from_the_current_directory(self):
        home = tempfile.mkdtemp(); marker = os.path.join(home, "RAN")
        script(os.path.join(home, "ls"), f"touch {marker}\nexec /bin/ls \"$@\"\n")
        s = Session(home=home, env={"PATH": ".:/usr/bin:/bin"}); self.addCleanup(s.close)
        s.send("alias\r", 0.6)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("alias ls='ls --color=auto'", s.text())      # the real ls was still found and tested

    def test_empty_path_entry_too(self):
        home = tempfile.mkdtemp(); marker = os.path.join(home, "RAN")
        script(os.path.join(home, "ls"), f"touch {marker}\nexec /bin/ls \"$@\"\n")
        s = Session(home=home, env={"PATH": ":/usr/bin:/bin"}); self.addCleanup(s.close)
        self.assertFalse(os.path.exists(marker))

    def test_which_safe_ignores_relative_entries(self):
        d = tempfile.mkdtemp(); script(os.path.join(d, "mycmd"), "echo x\n")
        o, _ = pysh_py("print(shell.which_safe('mycmd'))", cwd=d, env={"PATH": ".:/usr/bin"})
        self.assertEqual(o, "None")


class ResourceLimits(Base):
    def timed(self, script_text):
        t = time.time(); r = run(script_text); return r, time.time() - t

    def test_huge_power_returns_at_once(self):
        r, dt = self.timed("echo $((9**9**9))")
        self.assertEqual(r.rc, 0); self.assertLess(dt, 5)
        self.assertRegex(r.out.strip(), r"^-?\d+$")

    def test_arithmetic_wraps_like_a_64_bit_shell(self):
        self.assertOut("echo $((2**63)) $((2**64)) $((9223372036854775807 + 1)) $((-9223372036854775807 - 2))",
                       "-9223372036854775808 0 -9223372036854775808 9223372036854775807")

    def test_shifts_are_bounded(self):
        r, dt = self.timed("echo $((1 << 100000000000)) $((1 << 62)) $((-8 >> 1))")
        self.assertLess(dt, 5); self.assertEqual(r.out.split()[1:], ["4611686018427387904", "-4"])

    def test_huge_literals_are_handled(self):
        r, dt = self.timed("echo $((" + "9" * 5000 + " + 1))\necho $((" + "9" * 400 + " + 1))\necho after")
        self.assertLess(dt, 5)
        self.assertNotIn("Traceback", r.err); self.assertIn("after", r.out)

    def test_huge_brace_range_is_refused(self):
        r, dt = self.timed("echo {1..99999999} > /dev/null\necho after")
        self.assertLess(dt, 5)
        self.assertIn("brace expansion too large", r.err); self.assertIn("after", r.out)

    def test_nested_braces_cannot_explode(self):
        r, dt = self.timed("echo {1..1000}{1..1000}{1..1000} > /dev/null\necho after")
        self.assertLess(dt, 20)
        self.assertIn("brace expansion too large", r.err); self.assertIn("after", r.out)

    def test_normal_ranges_still_work(self):
        self.assertOut("echo {1..5} {a..c}", "1 2 3 4 5 a b c")


class CommandNotFound(unittest.TestCase):
    """A program that can't be started must not leave the shell without its terminal (it used to exit)."""

    def test_missing_command_with_arguments_keeps_the_shell(self):
        for line in ("nosuchcmd arg", "!", "true && nosuchcmd arg", "nosuchcmd | cat", "echo x | nosuchcmd arg"):
            with self.subTest(line=line):
                s = Session(); self.addCleanup(s.close)
                s.send(line + "\r", 0.8); s.send("echo alive-after\r", 0.8)
                self.assertTrue(s.alive())
                self.assertIn("\nalive-after", s.text())

    def test_permission_denied_keeps_the_shell(self):
        home = tempfile.mkdtemp(); p = os.path.join(home, "noexec")
        with open(p, "w") as f: f.write("x")
        s = Session(home=home); self.addCleanup(s.close)
        s.send("./noexec arg\r", 0.8); s.send("echo alive-after\r", 0.8)
        self.assertTrue(s.alive()); self.assertIn("\nalive-after", s.text())


class MalformedInput(unittest.TestCase):
    BAD = ["echo $(", "echo ${", 'echo "$(', "x = (", "$(", "!(", "![", "$[", "echo $((", "echo $((1+", "cat <<",
           "echo @(", "% $(", "echo `", "echo \\", "))", "(("]

    def test_repl_survives_every_malformed_line(self):
        s = Session(rows=30, cols=80); self.addCleanup(s.close)
        for line in self.BAD:
            s.send(line + "\r", 0.4)
            s.send("\x03", 0.2)                                  # abandon any continuation prompt
            self.assertTrue(s.alive(), f"shell exited on {line!r}")
        s.send("echo still-alive\r", 0.6)
        self.assertIn("still-alive", s.text())
        self.assertNotIn("Traceback", s.text())

    def test_unbalanced_substitution_reports_a_syntax_error(self):
        s = Session(); self.addCleanup(s.close)
        s.send("echo $(\r", 0.6)
        self.assertIn("syntax error", s.text()); self.assertTrue(s.alive())

    def test_script_continues_after_a_malformed_line(self):
        r = run("echo $(\necho next")
        self.assertIn("syntax error", r.err); self.assertIn("next", r.out)


if __name__ == "__main__":
    unittest.main()

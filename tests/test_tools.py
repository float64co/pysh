"""`explain`, `bash_env` and the opt-in `quiet_failures`."""
import os, shutil, subprocess, tempfile, textwrap, unittest

from helpers import PYSH
from term import Session


class Case(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="pysh-test-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, name, text):
        with open(os.path.join(self.d, name), "w") as f: f.write(textwrap.dedent(text))
        return os.path.join(self.d, name)

    def sh(self, script, env=None, *opts):
        path = self.write("t.pysh", script.lstrip("\n"))
        return subprocess.run([PYSH, *opts, path], capture_output=True, text=True, timeout=60, cwd=self.d,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, HOME=self.d, **(env or {})))


class Explain(Case):
    def explain(self, line, setup=""):
        r = self.sh(f"{setup}\nexplain {line!r}\n".replace("explain \"", "explain \""))
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        return r.stdout

    def has(self, text, *pieces):
        for piece in pieces: self.assertIn(piece, text)

    def test_python_lines_and_why(self):
        self.has(self.explain("x = 6 * 7"), "read as: Python", "why:")
        self.has(self.explain("for i in range(3):"), "read as: Python", "`for` is a Python keyword")
        self.has(self.explain("flags | mask", "flags = mask = 1"), "read as: Python", "every name in it is defined", "`flags` is a Python object (int)")
        self.has(self.explain("print(1)"), "read as: Python", "hint:    `print` is also a program") if shutil.which("print") else None
        self.has(self.explain("@property"), "read as: Python", "decorator")

    def test_shell_lines_and_why(self):
        self.has(self.explain("date"), "read as: shell command", "`date` is not defined", "program", shutil.which("date"))
        self.has(self.explain("cd /tmp"), "`cd` is a shell builtin", "builtin")
        self.has(self.explain("ls -l"), "read as: shell command", shutil.which("ls"))
        self.has(self.explain("% x = 1"), "leading `%`")
        self.has(self.explain("zzabsent one two"), "read as: shell command", "NOT FOUND")
        self.has(self.explain("zzabsent -x"), "read as: Python", "not defined, so it would raise NameError")

    def test_pipelines_stage_by_stage(self):
        out = self.explain("p(repo) | /usr/bin/tr a b", "p = print\nrepo = 1")
        self.has(out, "as:      @(p(repo)) | /usr/bin/tr a b", "1. Python source", "2. program", "status:  that of the last stage")
        out = self.explain("ps aux | raw=@tee | n=@len")
        self.has(out, "2. capture", "keeps the raw text in `raw` and shows it", "3. Python, then assign", "result in `n`")
        out = self.explain("echo a | @f | @tee | @nosuch.thing", "def f(s): pass")
        self.has(out, "gets the stream; runs in a forked copy", "3. tee", "NOT DEFINED")
        out = self.explain("echo a | @(cmd) -l", "cmd = 'wc'")
        self.has(out, "otherwise spliced in as words (not evaluated here)") if "@( )" in out else self.has(out, "chosen when the line runs")

    def test_lists_redirections_aliases_and_expansions(self):
        out = self.explain("cd /tmp && ll *.py > out.txt 2>&1 || echo $(date) &", "aliases['ll'] = 'ls -l'")
        self.has(out, "alias:   ll -> ls -l", "then, only if that succeeded:", "then, only if that failed:", "> out.txt",
                 "fd 2 goes where fd 1 goes", "runs in the background", "first:   each $( )", "globs:")
        self.has(self.explain("X=1 env | head -1", "set_pipefail = True"), "with X=1", "set_pipefail is on")

    def test_nothing_is_run(self):
        setup = "def boom(*a): open('ran', 'w').close()"
        for line in ("@(boom()) | cat", "boom() | cat", "touch ran2", "echo $(touch ran3)", "echo hi > ran4", "x=@boom < t.pysh",
                     "@boom(1) | cat", "echo | @(boom())", "VAR=$(touch ran5) true"):
            self.explain(line, setup)
        self.assertEqual(sorted(os.listdir(self.d)), ["t.pysh"])

    def test_properties_are_not_executed(self):
        setup = "class C:\n    @property\n    def p(self): open('ran', 'w').close(); return len\nc = C()"
        self.explain("echo | @c.p", setup)
        self.assertFalse(os.path.exists(os.path.join(self.d, "ran")))

    def test_errors_and_usage(self):
        self.has(self.explain("echo 'unterminated"), "syntax error")
        r = self.sh("explain\necho alive\n")
        self.assertIn("usage: explain", r.stderr); self.assertEqual(r.stdout, "alive\n")

    def test_hostile_text_is_neutralised(self):
        r = self.sh("explain \"echo '\\x1b]0;pwned\\x07'\"\n".replace("\\x1b", "\x1b").replace("\\x07", "\x07"))
        self.assertNotIn("\x1b]0;pwned", r.stdout)

    def test_interactive(self):
        s = Session(rows=30, cols=100, home=self.d); self.addCleanup(s.close)
        s.send("explain 'ls | wc -l'\r", 1.0)
        self.has(s.text(), "read as: shell command", "1. program", "2. program")


class BashEnv(Case):
    SCRIPT = """
        export NEW="a b"
        export MULTI=$'one\\ntwo'
        unset GONE
        export CHANGED=new
        PATH="$PATH:/opt/zz-extra"
        not_exported=1
        alias zz=ls
        zzf() { :; }; export -f zzf
        echo "sourced $*"
    """

    def go(self, script, **env):
        return self.sh(script, env=dict({"GONE": "1", "CHANGED": "old"}, **env))

    def test_takes_over_changes(self):
        self.write("env.sh", self.SCRIPT)
        r = self.go("""
            import os
            bash_env env.sh x "y z"
            print([os.environ.get(k) for k in ("NEW", "MULTI", "GONE", "CHANGED", "not_exported")])
            print(os.environ["PATH"].split(":")[-1], [k for k in os.environ if k.startswith("BASH_FUNC") or k == "PYSH_BASH_ENV_OUT"])
            """.replace("\n            ", "\n"))
        self.assertEqual(r.stdout, "sourced x y z\n['a b', 'one\\ntwo', None, 'new', None]\n/opt/zz-extra []\n", r.stderr)

    def test_dry_run_shows_and_changes_nothing(self):
        self.write("env.sh", self.SCRIPT)
        r = self.go("bash_env -n env.sh\necho [$NEW][$GONE][$CHANGED]\n")
        self.assertIn("would set NEW=a b", r.stdout)
        self.assertIn("would set PATH: adds /opt/zz-extra\n", r.stdout)
        self.assertIn("would unset GONE", r.stdout)
        self.assertTrue(r.stdout.endswith("[][1][old]\n"), r.stdout)

    def test_status_and_exit_inside_the_file(self):
        self.write("env.sh", "export A=1\nexit 7\nexport B=2\n")
        r = self.go("bash_env env.sh\necho $? [$A][$B]\n")
        self.assertEqual(r.stdout, "7 [1][]\n", r.stderr)
        self.write("strict.sh", "set -e\nexport S=1\nfalse\nexport T=2\n")
        self.assertEqual(self.go("bash_env strict.sh\necho $? [$S][$T]\n").stdout, "1 [1][]\n")

    def test_commands_and_a_file_named_like_a_program(self):
        self.write("ls", "export FROM_LOCAL=yes\n")
        r = self.go("bash_env ls\necho $FROM_LOCAL\nbash_env -c 'export FROM_C=\"$1\"' arg\necho $FROM_C\n")
        self.assertEqual(r.stdout, "yes\narg\n", r.stderr)

    def test_BASH_ENV_is_not_run(self):
        self.write("evil.sh", "export EVIL=1\n")
        r = self.go("bash_env -c true\necho [$EVIL]\n", BASH_ENV=os.path.join(self.d, "evil.sh"))
        self.assertEqual(r.stdout, "[]\n", r.stderr)

    def test_errors_change_nothing(self):
        r = self.go("bash_env /no/such\necho $?\nbash_env\necho $?\nbash_env -z x\necho $?\nbash_env -c 'kill -9 $$'\necho [$CHANGED]\n")
        self.assertEqual(r.stdout, "1\n2\n2\n[old]\n", r.stderr)
        self.assertIn("no such file", r.stderr); self.assertIn("nothing changed", r.stderr)

    def test_from_pyshrc_and_new_PATH_is_used(self):
        os.makedirs(os.path.join(self.d, "tools"))
        os.chmod(self.write("tools/zz-fromtools", "#!/bin/sh\necho found-it\n"), 0o755)
        self.write("env.sh", 'export PATH="$HOME/tools:$PATH"\n')
        self.write(".pyshrc", "bash_env ~/env.sh\n")
        s = Session(rows=30, cols=100, home=self.d); self.addCleanup(s.close)
        s.send("zz-fromtools\r", 1.0)
        self.assertIn("\nfound-it", s.text())


class QuietFailures(Case):
    BODY = "import json\nsh -c 'exit 22' | @json.load\necho status $?\nsh -c 'exit 22' | d=@json.load\n"

    def test_off_by_default(self):
        r = self.sh(self.BODY)
        self.assertEqual(r.stderr.count("Traceback"), 2, r.stderr)

    def test_no_traceback_when_an_earlier_command_failed(self):
        r = self.sh("quiet_failures = True\n" + self.BODY)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.stdout, "status 1\n")
        lines = r.stderr.strip().split("\n")
        self.assertRegex(lines[0], r"@json\.load: no result, because sh exited with status 22")
        self.assertRegex(lines[1], r"d not assigned: sh exited with status 22")
        self.assertEqual(len(lines), 2)

    def test_real_python_errors_still_show(self):
        r = self.sh("quiet_failures = True\nimport json\necho nope | @json.load\necho nope | @json.loads\n"
                    "def bad(f): raise KeyError('mine')\ntrue | @bad\n")
        self.assertEqual(r.stderr.count("Traceback"), 3, r.stderr)
        self.assertIn("did you mean @json.load?", r.stderr)

    def test_finding_nothing_is_not_an_earlier_failure(self):
        r = self.sh("quiet_failures = True\nimport json\necho a | grep zzz | d=@json.load\n")
        self.assertIn("JSONDecodeError", r.stderr)

    def test_traceback_still_follows_a_stderr_redirect(self):
        r = self.sh("quiet_failures = True\nimport json\necho x | @json.load 2> err.txt\n")
        self.assertEqual(r.stderr, "")
        with open(os.path.join(self.d, "err.txt")) as f: self.assertIn("JSONDecodeError", f.read())
        r = self.sh("quiet_failures = True\nimport json\nr = !(echo x | @json.load)\nprint('JSONDecodeError' in r.err)\n")
        self.assertEqual(r.stdout, "True\n", r.stderr)


if __name__ == "__main__":
    unittest.main()

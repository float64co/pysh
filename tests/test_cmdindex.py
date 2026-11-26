"""The in-memory index of $PATH names: typing must not search $PATH, and a stale index must never misread a line."""
import os, shutil, subprocess, tempfile, time, unittest

from helpers import PYSH
from term import Session

# makes every real $PATH search slow and counted, from inside the shell
SLOW_WHICH = """
import shutil, time
searches = []
_real = shutil.which
def which(*a, **kw):
    searches.append(a[0]); time.sleep(1.0); return _real(*a, **kw)
shutil.which = which
"""


class Case(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="pysh-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)

    def program(self, name, say="ran", directory=None, mode=0o755):
        path = os.path.join(directory or self.bin, name)
        with open(path, "w") as f: f.write(f"#!/bin/sh\necho {say} \"$@\"\n")
        os.chmod(path, mode)
        return path

    def start(self, rc=""):
        with open(os.path.join(self.home, ".pyshrc"), "w") as f: f.write(rc)
        s = Session(rows=40, cols=100, home=self.home, env={"PATH": self.bin + os.pathsep + "/usr/bin:/bin"})
        self.addCleanup(s.close)
        s.pump(0.5)
        return s

    def ask(self, s, line, wait=0.8):
        s.send("clear\r", 0.3)
        s.send(line + "\r", wait)
        lines = s.text().rstrip().split("\n")
        return "\n".join(lines[1:-1])

    def known(self, s, name):
        return self.ask(s, f"print(shell.command_index.lookup({name!r}))")


class Index(Case):
    def test_finds_executables_only(self):
        self.program("zz-tool"); self.program("zz-data", mode=0o644); os.makedirs(os.path.join(self.bin, "zz-dir"))
        s = self.start()
        for name, expected in (("zz-tool", "True"), ("zz-data", "False"), ("zz-dir", "False"), ("zz-none", "False")):
            self.assertEqual(self.known(s, name), expected, name)

    def test_installs_and_removals_are_seen_at_the_next_prompt(self):
        s = self.start()
        self.assertEqual(self.known(s, "zz-new"), "False")
        path = self.program("zz-new")
        self.ask(s, "true")
        self.assertEqual(self.known(s, "zz-new"), "True")
        os.unlink(path)
        self.assertEqual(self.known(s, "zz-new"), "False")                # a hit is checked on disk, even before a refresh

    def test_changing_PATH_is_picked_up(self):
        other = os.path.join(self.home, "other"); os.makedirs(other)
        self.program("zz-other", directory=other)
        s = self.start()
        self.assertEqual(self.known(s, "zz-other"), "False")
        self.ask(s, f"export PATH={other}:$PATH")
        self.assertEqual(self.known(s, "zz-other"), "True")

    def test_a_line_run_before_the_index_knows_still_works(self):
        s = self.start()
        s.pump(0.3)
        self.program("zz-late", say="late-program")             # installed after this prompt was drawn
        self.assertIn("late-program x", self.ask(s, "zz-late x"))

    def test_a_pipeline_into_a_program_the_index_does_not_know_yet(self):
        s = self.start()
        self.program("zz-pager", say="paged")
        self.assertIn("paged", self.ask(s, "[1, 2] | zz-pager"))

    def test_scripts_do_not_use_it(self):
        r = subprocess.run([PYSH, "-c", "print(shell.command_index.started, len(shell.command_index.dirs))"],
                           capture_output=True, text=True, env=dict(os.environ, HOME=self.home))
        self.assertEqual(r.stdout.split(), ["False", "0"], r.stderr)

    def test_tab_completes_command_names(self):
        self.program("zz-complete-me")
        s = self.start()
        s.send("zz-compl\t", 0.8)
        self.assertTrue(s.text().rstrip().endswith("zz-complete-me"), s.text())


class NoSearchWhileTyping(Case):
    """The point of the index: with every real $PATH search taking a second, typing stays instant."""
    def test_typing_never_searches(self):
        s = self.start(SLOW_WHICH)
        s.send("searches.clear()\r", 0.5)
        start = time.time()
        for line in ("nosuchprogram --flag", "kubectl get pods", "repo | lesz | baz", "x = y + zz", "zzz"):
            for ch in line: s.send(ch, 0.02)
            s.pump(0.2)
            self.assertTrue(s.text().rstrip().endswith(line), s.text())          # drawn already
            s.send("\x15", 0.1)
        self.assertLess(time.time() - start, 8)
        self.assertEqual(self.ask(s, "print(searches)"), "[]")

    def test_entering_an_unknown_name_confirms_with_one_real_search(self):
        s = self.start(SLOW_WHICH)
        s.send("searches.clear()\r", 0.5)
        self.ask(s, "zzabsent -x", 3.5)                      # could be Python, so it matters whether zzabsent is a program
        self.assertIn("zzabsent", self.ask(s, "print(set(searches))"))


class KeysAndThreads(Case):
    def test_threads_run_while_the_shell_waits_for_a_key(self):
        s = self.start()
        s.send("import threading, time\r", 0.4); s.send("ticks = []\r", 0.4)
        s.send("threading.Thread(target=lambda: [(time.sleep(0.05), ticks.append(1)) for _ in range(20)], daemon=True).start()\r", 0.3)
        time.sleep(1.8)                                            # idle at the prompt
        self.assertEqual(self.ask(s, "print(len(ticks))"), "20")


if __name__ == "__main__":
    unittest.main()

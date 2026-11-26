"""Highlighting and Tab completion for `@name` stages."""
import os, re, shutil, subprocess, tempfile, textwrap, unittest

from helpers import PYSH
from term import Session

CYAN, RED, YELLOW, BLUE = "\x1b[36m", "\x1b[31m", "\x1b[33m", "\x1b[34m"

SETUP = """
import json, re, sys
from pyshlib import where, freq, table
rows = ["a"]
def errors(stream): pass
class Box:
    @property
    def trap(self): open('ran', 'w').close(); return len
    @staticmethod
    def stat(stream): pass
    @classmethod
    def clas(cls, stream): pass
box = Box()
"""


class Case(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="pysh-test-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def ask(self, lines, expr):
        """-> {line: repr of expr(line)} evaluated inside pysh"""
        script = SETUP + f"for line in {lines!r}: print(repr({expr}))\n"
        path = os.path.join(self.d, "t.pysh")
        with open(path, "w") as f: f.write(script)
        r = subprocess.run([PYSH, path], capture_output=True, text=True, timeout=60, cwd=self.d, env=dict(os.environ, HOME=self.d))
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(zip(lines, (eval(out) for out in r.stdout.splitlines())))


class Highlight(Case):
    def hl(self, *lines):
        got = self.ask(list(lines), "shell.highlight(line)")
        for line, coloured in got.items():                 # colour never changes the text or its width
            self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", coloured), line)
        return got

    def test_known_names_cyan_unknown_red(self):
        got = self.hl("cat x | @json.load", "cat x | @errors", "cat x | @len", "cat x | @nosuch", "cat x | @json.nope", "cat x | @rows")
        for line in ("cat x | @json.load", "cat x | @errors", "cat x | @len"):
            self.assertIn(CYAN + line.split("| ")[1], got[line], line)
        for line in ("cat x | @nosuch", "cat x | @json.nope", "cat x | @rows"):        # a list can't take input
            self.assertIn(RED + line.split("| ")[1], got[line], line)

    def test_a_value_is_fine_as_the_source(self):
        self.assertIn(CYAN + "@rows", self.hl("@rows | sort")["@rows | sort"])

    def test_assignment_and_capture_forms(self):
        got = self.hl("cat x | data=@json.load", "ps | raw=@ | n=@len", "ps | raw=@tee | @errors", "ps | x=@nosuch", "d=@json.load < c.json")
        self.assertIn("data=" + CYAN + "@json.load", got["cat x | data=@json.load"])
        self.assertIn("raw=" + CYAN + "@\x1b", got["ps | raw=@ | n=@len"])
        self.assertIn("raw=" + CYAN + "@tee", got["ps | raw=@tee | @errors"])
        self.assertIn("x=" + RED + "@nosuch", got["ps | x=@nosuch"])
        self.assertIn("d=" + CYAN + "@json.load", got["d=@json.load < c.json"])

    def test_inside_the_brackets_is_highlighted_as_python(self):
        line = "ls | @where(lambda l: 'x|y' in l and len(l) > 3) | wc"
        coloured = self.hl(line)[line]
        self.assertIn(BLUE + "lambda", coloured)
        self.assertIn(YELLOW + "'x|y'", coloured)                      # the | inside it is not a pipe
        self.assertIn(CYAN + "3", coloured)
        self.assertTrue(coloured.endswith(" wc"), coloured)

    def test_unfinished_lines_and_splices(self):
        got = self.hl("ls | @where(lambda l: 'ab", "ls | @(", "ls @(rows) x", "ls | @json.", "x | @", "ls |@errors|wc", "a=@", "@")
        self.assertIn(YELLOW + "'ab", got["ls | @where(lambda l: 'ab"])
        self.assertIn(CYAN + "@", got["ls @(rows) x"])

    def test_ordinary_arguments_are_left_alone(self):
        for line in ("echo @nosuch", "echo @f(1) a@b", "git log @{u}", "X=@nosuch env", "echo '| @nosuch'"):
            with self.subTest(line=line):
                self.assertNotIn(RED, self.hl(line)[line])
                self.assertNotIn(CYAN + "@", self.hl(line)[line])

    def test_nothing_is_executed(self):
        got = self.hl("echo | @box.trap", "echo | @box.trap.x", "echo | @box.stat", "echo | @box.clas", "echo | @(open('ran', 'w'))")
        self.assertIn(CYAN + "@box.stat", got["echo | @box.stat"])
        self.assertIn(CYAN + "@box.clas", got["echo | @box.clas"])
        self.assertNotIn(RED, got["echo | @box.trap"])                  # unknown without running it: not marked wrong
        self.assertFalse(os.path.exists(os.path.join(self.d, "ran")))


class Complete(Case):
    def comp(self, *lines):
        # as the editor does: a single match replaces the line (with its space), else the common prefix
        return self.ask(list(lines), "(lambda t: (t[1][0][0] if len(t[1]) == 1 else t[0], [d for _, d, _ in t[1]]))(shell.complete(line))")

    def test_names_after_a_pipe(self):
        got = self.comp("cat x | @", "cat x | @err", "cat x | @js", "cat x | @json.lo", "cat x | @zzz", "ls |@err")
        offered = got["cat x | @"][1]
        for name in ("@errors", "@freq", "@lines", "@json.", "@tee", "@where"): self.assertIn(name, offered)
        for name in ("@rows", "@sh", "@Result", "@abs", "@aliases"): self.assertNotIn(name, offered)
        self.assertEqual(got["cat x | @err"][0], "cat x | @errors ")
        self.assertEqual(got["cat x | @js"][0], "cat x | @json.")                  # a module: no space, keep going
        self.assertEqual(got["cat x | @json.lo"], ("cat x | @json.load", ["@json.load", "@json.loads"]))
        self.assertEqual(got["cat x | @zzz"], ("cat x | @zzz", []))
        self.assertEqual(got["ls |@err"][0], "ls |@errors ")

    def test_builtins_once_something_is_typed_and_values_as_a_source(self):
        got = self.comp("ls | @sort", "@ro", "ls | @ro", "@rows", "echo hi; @err")
        self.assertIn("@sorted", got["ls | @sort"][1])
        self.assertIn("@rows", got["@ro"][1])
        self.assertNotIn("@rows", got["ls | @ro"][1])
        self.assertEqual(got["echo hi; @err"][0], "echo hi; @errors ")

    def test_assignment_forms(self):
        got = self.comp("ps | data=@json.loa", "ps | x=@err", "ps | x=@t")
        self.assertEqual(got["ps | data=@json.loa"][0], "ps | data=@json.load")
        self.assertEqual(got["ps | x=@err"][0], "ps | x=@errors ")
        self.assertIn("@tee", got["ps | x=@t"][1])

    def test_inside_brackets(self):
        got = self.comp("ls | @where(lambda l: json.du", "ls | @(le", "ls | @(box.sta", "ls | @freq(ro")
        self.assertEqual(got["ls | @where(lambda l: json.du"][0], "ls | @where(lambda l: json.dump")
        self.assertEqual(got["ls | @(le"][0], "ls | @(len")
        self.assertEqual(got["ls | @(box.sta"][0], "ls | @(box.stat")
        self.assertIn("rows", got["ls | @freq(ro"][1])

    def test_arguments_complete_as_before(self):
        open(os.path.join(self.d, "@file"), "w").close()
        got = self.comp("echo @fi", "echo @err")
        self.assertEqual(got["echo @fi"][0], "echo @file ")
        self.assertEqual(got["echo @err"][1], [])

    def test_nothing_is_executed(self):
        self.comp("echo | @box.tr", "echo | @box.trap.", "echo | @box.", "ls | @(box.trap.")
        self.assertFalse(os.path.exists(os.path.join(self.d, "ran")))


class InTheEditor(Case):
    def start(self):
        s = Session(rows=30, cols=90, home=self.d); self.addCleanup(s.close)
        s.send("import json\r", 0.6)
        return s

    def test_tab_completes_and_the_line_runs(self):
        s = self.start()
        s.send("echo '[7]' | @js\t", 0.8)
        s.send("loa\t", 0.8)                                       # two matches: completes the common part, shows both
        self.assertIn("echo '[7]' | @json.load\n@json.load", s.text())
        s.send("\x1b", 0.5); s.send("\x15", 0.4)
        s.send("echo '[7]' | @json.load\r", 0.8)
        self.assertIn("\n7\n", s.text() + "\n")

    def test_menu_shows_descriptions_and_colour_follows_typing(self):
        s = self.start()
        s.send("def shout(stream):\r", 0.4); s.send("    '''say it loudly'''\r", 0.4); s.send("\r", 0.6)
        s.send("echo x | @sho\t", 0.8)
        self.assertTrue(s.text().rstrip().endswith("echo x | @shout"), s.text())
        s.send("\x15echo x | @\t", 0.8)
        self.assertIn("say it loudly", s.text())
        s.send("\x1b", 0.5); s.send("\x15", 0.4)
        s.raw = b""
        s.send("echo | @shou", 0.8)
        self.assertIn(RED + "@shou", s.raw.decode(errors="replace"))
        s.raw = b""
        s.send("t", 0.8)
        self.assertIn(CYAN + "@shout", s.raw.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()

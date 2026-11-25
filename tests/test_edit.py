"""Alt-E: edit the line in $EDITOR and run what was saved; multi-line entries in history."""
import os, shutil, sys, tempfile, unittest

from term import Session

ALT_E, UP, CTRL_U, CTRL_E, CTRL_A = "\x1be", "\x1b[A", "\x15", "\x05", "\x01"

# A stand-in editor. It records what it was given; then, if $HOME/save exists, writes that over the
# file (a "save"), and exits with the status in $HOME/status.
FAKE = r'''#!%s
import os, sys
home, path = os.environ["HOME"], sys.argv[-1]
with open(path) as f: given = f.read()
with open(home + "/given", "w") as f: f.write(given)
with open(home + "/path", "w") as f: f.write("%%o %%s %%s" %% (os.stat(path).st_mode & 0o777, path, " ".join(sys.argv[1:-1])))
if os.path.exists(home + "/save"):
    with open(home + "/save") as f: new = f.read()
    with open(path, "w") as f: f.write(new)
if os.path.exists(home + "/status"):
    with open(home + "/status") as f: sys.exit(int(f.read()))
''' % sys.executable


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="pysh-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.write("fake-editor", FAKE); os.chmod(os.path.join(self.home, "fake-editor"), 0o755)

    def write(self, name, text):
        with open(os.path.join(self.home, name), "w") as f: f.write(text)

    def read(self, name):
        with open(os.path.join(self.home, name)) as f: return f.read()

    def start(self, editor=None, **env):
        s = Session(rows=40, cols=90, home=self.home, env=dict(env, EDITOR=editor or f"{self.home}/fake-editor"))
        self.addCleanup(s.close)
        return s

    def send(self, s, data, wait=0.8):
        s.send(data, wait)
        return s.text()


class EditInEditor(Base):
    def test_runs_the_saved_block_at_once_and_shows_it(self):
        s = self.start()
        self.write("save", "total = 0\nfor i in range(4):\n    total += i\nprint('total is', total)\n")
        text = self.send(s, "print('typed')" + ALT_E, 1.2)
        self.assertEqual(self.read("given"), "print('typed')\n")
        self.assertIn("… total = 0\n… for i in range(4):\n…     total += i\n… print('total is', total)\ntotal is 6\n", text)
        self.assertIn("\n6", self.send(s, "total\r"))                       # it ran in the session

    def test_shell_lines_run_one_by_one(self):
        s = self.start()
        self.write("save", "export A=1\necho first $A\nfor n in (2, 3):\n    print('py', n)\necho last\n")
        self.assertIn("first 1\npy 2\npy 3\nlast\n", self.send(s, ALT_E, 1.2))

    def test_single_python_statement_still_echoes_values(self):
        s = self.start()
        self.write("save", "for i in (41, 42):\n    i\n")
        self.assertIn("\n41\n42\n", self.send(s, ALT_E, 1.2))

    def test_a_single_line_runs_as_if_typed(self):
        s = self.start()
        self.write("save", "6 * 7\n")
        self.assertIn("\n42\n", self.send(s, ALT_E, 1.2))

    def test_quitting_without_saving_runs_nothing_and_keeps_the_line(self):
        s = self.start()
        text = self.send(s, "echo do-not-run" + ALT_E, 1.2)
        self.assertNotIn("\ndo-not-run", text)
        self.assertIn("echo do-not-run", self.send(s, UP).split("\n")[-1])

    def test_saving_an_unchanged_line_runs_it(self):
        s = self.start()
        self.write("save", "echo unchanged\n")
        self.assertIn("\nunchanged\n", self.send(s, "echo unchanged" + ALT_E, 1.2))

    def test_empty_file_and_failing_editor_run_nothing(self):
        s = self.start()
        self.write("save", "\n\n   \n")
        self.assertNotIn("…", self.send(s, "echo no" + ALT_E, 1.2))
        self.write("save", "echo should-not-run\n"); self.write("status", "1")
        text = self.send(s, ALT_E, 1.2)
        self.assertIn("editor exited with status 1; nothing run", text)
        self.assertNotIn("\nshould-not-run", text)

    def test_missing_editor_is_reported_and_the_shell_lives_on(self):
        s = self.start(editor="no-such-editor-xyz")
        text = self.send(s, "echo x" + ALT_E, 1.2)
        self.assertIn("no-such-editor-xyz: command not found", text)
        self.assertIn("\nalive\n", self.send(s, "echo alive\r") + "\n")

    def test_editor_with_arguments_private_temp_file_removed_afterwards(self):
        s = self.start(editor=f"{self.home}/fake-editor --wait 'two words'")
        self.write("save", "echo ok\n")
        self.send(s, ALT_E, 1.2)
        mode, path, args = self.read("path").split(" ", 2)
        self.assertEqual((mode, args), ("600", "--wait two words"))
        self.assertTrue(path.endswith(".pysh"))
        self.assertFalse(os.path.exists(path))

    def test_includes_continuation_lines_already_typed(self):
        s = self.start()
        self.send(s, "for i in range(3):\r", 0.5)
        self.send(s, "    print(i)" + ALT_E, 1.2)
        self.assertEqual(self.read("given"), "for i in range(3):\n    print(i)\n")

    def test_leaves_last_status_alone_and_ctrl_e_is_still_end_of_line(self):
        s = self.start()
        self.send(s, "echo ab")
        self.send(s, CTRL_A + CTRL_E + "c", 0.5)
        self.assertIn("\nabc\n", self.send(s, "\r") + "\n")
        self.assertFalse(os.path.exists(os.path.join(self.home, "given")))

    def test_works_after_changing_EDITOR_in_the_session(self):
        s = self.start(editor="false")
        self.write("save", "echo switched\n")
        self.send(s, f"export EDITOR={self.home}/fake-editor\r")
        self.assertIn("\nswitched\n", self.send(s, ALT_E, 1.2))


class DefaultEditor(Base):
    def test_is_vim_unless_already_set(self):
        saved = os.environ.pop("EDITOR", None)
        try:
            s = Session(rows=20, cols=80, home=self.home); self.addCleanup(s.close)
            self.assertIn("\nvim", self.send(s, "echo $EDITOR\r"))
        finally:
            if saved is not None: os.environ["EDITOR"] = saved
        self.assertIn("\nnano", self.send(self.start(editor="nano"), "echo $EDITOR\r"))

    @unittest.skipUnless(shutil.which("vim"), "needs vim")
    def test_with_real_vim(self):
        s = self.start(editor="vim -u NONE -i NONE -n")
        s.send("echo from" + ALT_E, 1.5)
        s.send("A-vim\x1b", 0.5)
        s.send(":wq\r", 1.5)
        self.assertIn("from-vim", s.raw.decode(errors="replace").split(":wq")[-1])
        s.send("echo not" + ALT_E, 1.5)
        s.send("A-run\x1b", 0.5)
        s.send(":q!\r", 1.5)
        s.send("echo after\r", 0.8)
        self.assertNotIn("\r\nnot-run", s.raw.decode(errors="replace"))
        self.assertIn("\r\nafter", s.raw.decode(errors="replace"))


class BlockHistory(Base):
    BLOCK = "for i in range(2):\n    print('block', i)\necho tail\n"

    def run_block(self, s):
        self.write("save", self.BLOCK)
        self.send(s, ALT_E, 1.2)

    def recalled(self, s):
        return self.send(s, UP, 0.6).split("\n")[-1]

    def test_recalled_on_one_line_and_rerun(self):
        s = self.start()
        self.run_block(s)
        self.assertIn("for i in range(2):⏎    print('block', i)⏎echo tail", self.recalled(s))
        text = self.send(s, "\r")
        self.assertEqual(text.count("\nblock 1\ntail\n"), 2, text)

    def test_recalled_line_clears_cleanly(self):
        s = self.start()
        self.run_block(s)
        before = s.text()
        self.send(s, UP, 0.6); text = self.send(s, CTRL_U, 0.6)
        self.assertEqual(text.rstrip(), before.rstrip())

    def test_alt_e_on_a_recalled_block_gives_the_editor_real_lines(self):
        s = self.start()
        self.run_block(s)
        os.remove(os.path.join(self.home, "save"))
        self.send(s, UP, 0.6); self.send(s, ALT_E, 1.2)
        self.assertEqual(self.read("given"), self.BLOCK)

    def test_survives_a_restart_as_one_entry(self):
        s = self.start()
        self.run_block(s)
        s.send("exit\r", 0.8)
        self.assertEqual(self.read(".pysh_history").split("\n")[0],
                         "\x1efor i in range(2):\\n    print('block', i)\\necho tail")
        t = self.start()
        self.send(t, UP, 0.5)                                       # "exit"
        self.assertIn("for i in range(2):⏎", self.recalled(t))
        self.assertIn("\nblock 1\ntail", self.send(t, "\r"))

    def test_typed_blocks_survive_a_restart_too(self):
        s = self.start()
        for line in ("for i in (7,):\r", "    print('typed', i)\r", "\r"): self.send(s, line, 0.5)
        s.send("exit\r", 0.8)
        t = self.start()
        self.send(t, UP, 0.5); self.send(t, UP, 0.5)
        self.assertIn("\ntyped 7", self.send(t, "\r"))

    def test_backslashes_and_old_history_lines_are_untouched(self):
        old = ["echo 'a\\nb'", r"printf 'x\\y'"]
        self.write(".pysh_history", "".join(l + "\n" for l in old))
        s = self.start()
        self.assertIn(old[1], self.recalled(s))
        self.assertIn(old[0], self.recalled(s))
        self.write("save", "print(r'c:\\new\\\\path')\necho z\n")
        self.send(s, CTRL_U + ALT_E, 1.2); s.send("exit\r", 0.8)
        t = self.start()
        self.send(t, UP, 0.5)
        os.remove(os.path.join(self.home, "save"))
        self.send(t, UP, 0.5); self.send(t, ALT_E, 1.2)
        self.assertEqual(self.read("given"), "print(r'c:\\new\\\\path')\necho z\n")


if __name__ == "__main__":
    unittest.main()

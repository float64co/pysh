"""Line-editor tests: pysh runs on a pty, its output is rendered by a small VT emulator (term.py)."""
import os, re, tempfile, unittest
from term import Session

HOST = os.uname().nodename.split(".")[0]
PROMPT = f"tester@{HOST} ~> "          # visible prompt in $HOME
PW = len(PROMPT)
UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
ENTER, CTRL = "\r", lambda c: chr(ord(c.lower()) - 96)


class EditorCase(unittest.TestCase):
    cols, rows, history = 40, 24, None

    def setUp(self):
        self.s = Session(rows=self.rows, cols=self.cols, history=self.history)
        self.addCleanup(self.s.close)

    # --- helpers
    def type(self, text, quiet=0.3): return self.s.send(text, quiet)

    def edit_text(self):
        """Prompt + buffer exactly as drawn, joined across wrapped rows (from the last prompt)."""
        ls = self.s.screen.lines()
        idx = max(i for i, l in enumerate(ls) if l.startswith("tester@") or l.startswith(PROMPT[:-1]))
        rows = []
        for l in ls[idx:]:
            rows.append(l)
        # un-wrap: every full-width row continues on the next one
        txt = ""
        for l in rows:
            txt += l if len(l) >= self.cols else l + "\n"
        return txt.rstrip("\n")

    def buffer(self):
        """Text after the prompt as drawn (trailing blanks are invisible on a screen)."""
        t = self.edit_text()
        return t[PW:] if t.startswith(PROMPT) else ""

    def cursor(self): return (self.s.screen.r, self.s.screen.c)

    def settle_cursor(self, n, timeout=10.0):
        import time
        a = PW + n
        end = time.time() + timeout
        while time.time() < end and self.cursor() != (a // self.cols, a % self.cols): self.s.pump(0.1, 0.4)

    def settle(self, expected, timeout=10.0):
        """Wait until the edit buffer shows `expected` (typing a long line can lag behind under load)."""
        import time
        end = time.time() + timeout
        while time.time() < end and self.buffer() != expected: self.s.pump(0.2, 0.5)

    def expect_cursor(self, n, start_row=0):
        """Cursor is where it should be after n columns of text following the prompt."""
        a = PW + n
        self.assertEqual(self.cursor(), (start_row + a // self.cols, a % self.cols))

    def run_cmd(self, cmd):
        self.type(cmd + ENTER, 0.5)
        return self.s.text()


class Basics(EditorCase):
    def test_prompt(self):
        self.assertTrue(self.s.text().startswith(PROMPT.rstrip()), self.s.text())

    def test_run_command(self):
        self.assertIn("\nhi", self.run_cmd("echo hi"))

    def test_python_expression_display(self):
        self.assertIn("\n42", self.run_cmd("6*7"))

    def test_backspace_and_insert(self):
        self.type("echo abXc")
        self.type(LEFT)
        self.type("\x7f")             # delete X
        self.assertEqual(self.buffer(), "echo abc")
        self.type("Z")
        self.assertEqual(self.buffer(), "echo abZc")

    def test_ctrl_a_e_k_u(self):
        self.type("hello world")
        self.type(CTRL("a")); self.assertEqual(self.cursor(), (0, PW))
        self.type(CTRL("e")); self.assertEqual(self.cursor(), (0, PW + 11))
        self.type(CTRL("a") + RIGHT * 5 + CTRL("k")); self.assertEqual(self.buffer(), "hello")
        self.type(CTRL("u")); self.assertEqual(self.buffer(), "")

    def test_ctrl_w_kills_word(self):
        self.type("echo one two"); self.type(CTRL("w"))
        self.assertEqual(self.buffer(), "echo one")
        self.assertEqual(self.cursor(), (0, PW + len("echo one ")))

    def test_ctrl_c_abandons_line(self):
        self.type("echo nope"); self.type(CTRL("c"))
        self.assertIn("^C", self.s.text())
        self.assertNotIn("\nnope", self.s.text())
        self.assertEqual(self.s.text().split("\n")[-1], PROMPT.rstrip())

    def test_ctrl_d_on_empty_exits(self):
        self.type(CTRL("d"), 0.3)
        self.assertTrue(self.s.wait_exit())

    def test_ctrl_d_with_text_deletes(self):
        self.type("abc" + LEFT + LEFT + CTRL("d"))
        self.assertEqual(self.buffer(), "ac")
        self.assertTrue(self.s.alive())

    def test_ctrl_l_clears_screen(self):
        self.run_cmd("echo one"); self.type(CTRL("l"))
        self.assertEqual(self.s.text(), PROMPT.rstrip())

    def test_multiline_python(self):
        self.type("for i in range(2):" + ENTER)
        self.type("    print(i)" + ENTER)
        self.type(ENTER, 0.5)
        self.assertIn("0\n1", self.s.text())

    def test_heredoc_at_prompt(self):
        self.type("cat <<EOF" + ENTER); self.type("typed body" + ENTER); self.type("EOF" + ENTER, 0.6)
        self.assertIn("typed body", self.s.text().split("EOF", 1)[1])

    def test_unicode_input(self):
        self.type("echo héllo")
        self.assertEqual(self.buffer(), "echo héllo")
        self.type("\x7f\x7f")
        self.assertEqual(self.buffer(), "echo hél"[:-1] if False else "echo hél")


class Wrapping(EditorCase):
    def test_long_line_wraps_and_cursor_follows(self):
        text = "echo " + "x" * 80
        self.type(text)
        self.assertEqual(self.buffer(), text)
        self.expect_cursor(len(text))

    def test_every_length_around_row_boundaries(self):
        # exercise exact-fit edges: the end of the text landing on the last column / first column
        for total in (self.cols - PW - 1, self.cols - PW, self.cols - PW + 1,
                      2 * self.cols - PW - 1, 2 * self.cols - PW, 2 * self.cols - PW + 1):
            with self.subTest(total=total):
                self.type(CTRL("u"))
                text = "a" * total
                self.type(text)
                self.assertEqual(self.buffer(), text)
                self.expect_cursor(total)
                # no stray duplicate rows
                self.assertEqual(sum(1 for l in self.s.screen.lines() if l.startswith("tester@")), 1)

    def test_backspace_across_row_boundary(self):
        self.type("a" * 60)
        for n in range(59, 0, -1):
            self.type("\x7f", 0.05)
            if n in (self.cols - PW, self.cols - PW + 1, self.cols - PW - 1, 30):
                self.settle("a" * n)                      # keys queue up faster than each repaint
                self.assertEqual(self.buffer(), "a" * n)
                self.expect_cursor(n)
        self.settle("a")
        self.assertEqual(self.buffer(), "a")

    def test_left_arrow_across_rows(self):
        self.type("b" * 70)
        for n in range(70, 0, -1):
            self.settle_cursor(n)
            self.expect_cursor(n)
            self.type(LEFT, 0.04)
        self.expect_cursor(0)

    def test_home_end_across_rows(self):
        self.type("c" * 70)
        self.type(CTRL("a")); self.expect_cursor(0)
        self.type(CTRL("e")); self.expect_cursor(70)

    def test_insert_in_middle_of_wrapped_line(self):
        self.type("d" * 70)
        self.type(CTRL("a") + RIGHT * 10 + "Z")
        self.assertEqual(self.buffer(), "d" * 10 + "Z" + "d" * 60)
        self.expect_cursor(11)

    def test_kill_to_end_clears_following_rows(self):
        self.type("e" * 90)
        self.type(CTRL("a") + RIGHT * 5 + CTRL("k"))
        self.assertEqual(self.buffer(), "e" * 5)
        self.assertEqual(len([l for l in self.s.screen.lines() if l.strip()]), 1)

    def test_kill_line_clears_screen(self):
        self.type("f" * 100); self.type(CTRL("u"))
        self.assertEqual(self.s.text(), PROMPT.rstrip())

    def test_wrapped_command_runs_intact(self):
        self.type("echo -n " + "g" * 100 + " | wc -c" + ENTER, 0.6)
        self.assertIn("\n100", self.s.text())

    def test_wrapped_line_at_bottom_of_screen(self):
        for _ in range(30): self.type("echo filler" + ENTER, 0.05)
        self.s.pump(1.0)                                   # let the shell finish the whole backlog first
        self.type("h" * 100, 0.3)
        self.settle("h" * 100)
        self.assertEqual(self.buffer(), "h" * 100)
        self.expect_cursor(100, start_row=self.cursor()[0] - (PW + 100) // self.cols)

    def test_wide_characters(self):
        self.type("漢" * 30)           # 60 columns
        self.assertEqual(self.buffer().replace("\n", ""), "漢" * 30)
        a = PW + 60
        self.assertEqual(self.cursor(), (a // self.cols, a % self.cols))
        self.type("\x7f" * 5)
        a = PW + 50
        self.assertEqual(self.cursor(), (a // self.cols, a % self.cols))

    def test_narrow_terminal(self):
        s = Session(rows=10, cols=20, history=None); self.addCleanup(s.close)
        s.send("echo " + "z" * 50)
        self.assertEqual(s.screen.r, (PW + 55) // 20)

    def test_suggestion_wrapping(self):
        pass


class WrappingWithSuggestion(EditorCase):
    history = ["echo " + "s" * 70]

    def test_suggestion_spans_rows_and_accepts(self):
        self.type("echo ss")
        self.assertEqual(self.buffer(), "echo " + "s" * 70)       # suggestion drawn after the cursor
        self.expect_cursor(7)
        self.type(RIGHT)
        self.assertEqual(self.buffer(), "echo " + "s" * 70)
        self.expect_cursor(75)
        self.type(ENTER, 0.8)
        self.assertIn("s" * 140, self.s.text().replace("\n", ""))   # command line, then its output

    def test_suggestion_disappears_on_enter(self):
        self.type("echo s")
        self.assertEqual(self.buffer(), "echo " + "s" * 70)       # suggestion visible while typing
        self.type(CTRL("c"))
        self.assertNotIn("s" * 70, self.s.text())                 # gone once the line is abandoned


class HistoryAndSuggestions(EditorCase):
    history = ["echo hello world", "ls -la /etc", "x = 5", "grep foo bar", "echo second hello"]

    def test_suggestion_shown_after_cursor(self):
        self.type("ech")
        self.assertEqual(self.buffer(), "echo second hello")
        self.assertEqual(self.cursor(), (0, PW + 3))

    def test_right_arrow_accepts_suggestion(self):
        self.type("ls"); self.type(RIGHT)
        self.assertEqual(self.buffer(), "ls -la /etc")
        self.assertEqual(self.cursor(), (0, PW + 11))

    def test_ctrl_f_accepts_suggestion(self):
        self.type("gr" + CTRL("f"))
        self.assertEqual(self.buffer(), "grep foo bar")

    def test_alt_f_accepts_one_word(self):
        self.type("echo" + "\x1bf")
        self.assertEqual(self.cursor()[1], PW + len("echo second"))

    def test_up_arrow_searches_by_prefix(self):
        self.type("echo" + UP)
        self.assertEqual(self.buffer(), "echo second hello")
        self.type(UP); self.assertEqual(self.buffer(), "echo hello world")
        self.type(UP); self.assertEqual(self.buffer(), "echo hello world")      # no older match
        self.type(DOWN); self.assertEqual(self.buffer(), "echo second hello")
        self.type(DOWN); self.assertEqual(self.cursor(), (0, PW + 4))           # back to what was typed

    def test_up_arrow_empty_walks_history(self):
        self.type(UP); self.assertEqual(self.buffer(), "echo second hello")
        self.type(UP); self.assertEqual(self.buffer(), "grep foo bar")

    def test_no_suggestion_for_non_matching(self):
        self.type("zzz")
        self.assertEqual(self.buffer(), "zzz")

    def test_history_is_saved(self):
        self.run_cmd("echo saved-entry")
        with open(os.path.join(self.s.home, ".pysh_history")) as f:
            self.assertIn("echo saved-entry\n", f.read())


class CtrlR(EditorCase):
    history = ["echo hello world", "ls -la /etc", "x = 5", "grep foo bar", "echo second hello", "git status"]

    def test_opens_menu_newest_first(self):
        self.type(CTRL("r"))
        lines = self.s.text().split("\n")[1:]
        self.assertEqual(lines[0].strip(), "git status")
        self.assertEqual(len(lines), 6)

    def test_typing_filters(self):
        self.type(CTRL("r") + "hel")
        self.assertEqual([l.strip() for l in self.s.text().split("\n")[1:]], ["echo second hello", "echo hello world"])

    def test_case_insensitive_substring(self):
        self.type(CTRL("r") + "LA /E")
        self.assertEqual([l.strip() for l in self.s.text().split("\n")[1:]], ["ls -la /etc"])

    def test_enter_inserts_without_running(self):
        self.type(CTRL("r") + "foo" + ENTER)
        self.assertEqual(self.buffer(), "grep foo bar")
        self.assertNotIn("command not found", self.s.text())
        self.assertEqual(self.s.text().count("\n"), 0)

    def test_arrows_move_selection(self):
        self.type(CTRL("r") + "echo" + DOWN + ENTER)
        self.assertEqual(self.buffer(), "echo hello world")

    def test_ctrl_r_again_cycles(self):
        self.type(CTRL("r") + "echo" + CTRL("r") + ENTER)
        self.assertEqual(self.buffer(), "echo hello world")

    def test_escape_cancels_and_restores(self):
        self.type("keep")
        self.type(CTRL("r") + "zzz")
        self.type("\x1b", 0.4)
        self.assertEqual(self.buffer(), "keep")
        self.assertEqual(self.s.text().count("\n"), 0)

    def test_no_matches(self):
        self.type(CTRL("r") + "nomatchatall")
        self.assertEqual(self.s.text().count("\n"), 0)
        self.type(ENTER)
        self.assertEqual(self.buffer(), "nomatchatall")

    def test_initial_query_is_current_line(self):
        self.type("git" + CTRL("r"))
        self.assertEqual([l.strip() for l in self.s.text().split("\n")[1:]], ["git status"])


class Completion(EditorCase):
    def setUp(self):
        super().setUp()
        for d in ("uniq_dir_abc",): os.makedirs(os.path.join(self.s.home, d), exist_ok=True)
        for f in ("fa1.txt", "fb2.txt", "has space.txt"):
            with open(os.path.join(self.s.home, f), "w") as fh: fh.write("x")

    def test_unique_directory_completes_with_slash(self):
        self.type("cd uniq_d\t")
        self.assertEqual(self.buffer(), "cd uniq_dir_abc/")

    def test_unique_file_adds_space(self):
        self.type("cat fa\t")
        self.assertEqual(self.buffer(), "cat fa1.txt")

    def test_common_prefix_then_menu_with_descriptions(self):
        self.type("cat f\t")
        txt = self.s.text()
        self.assertIn("fa1.txt", txt); self.assertIn("fb2.txt", txt)
        self.assertEqual(self.buffer().split("\n")[0], "cat f")
        self.assertRegex(txt, r"fa1\.txt\s+1B")                   # description: file size

    def test_tab_cycles_and_enter_runs_selection(self):
        self.type("cat f\t"); self.type("\t")
        self.assertTrue(self.buffer().split("\n")[0].startswith("cat fa1.txt"))
        self.type("\t")
        self.assertTrue(self.buffer().split("\n")[0].startswith("cat fb2.txt"))
        self.type(ENTER, 0.6)
        lines = self.s.text().split("\n")
        self.assertEqual(lines[0].rstrip(), PROMPT.rstrip() + " cat fb2.txt")   # menu gone, line run as selected
        self.assertTrue(lines[1].startswith("x"))                               # cat printed the file

    def test_enter_on_selected_command_runs_it_without_arguments(self):
        self.type("ec\t", 2.0)                 # menu of commands starting with "ec"
        self.type("\t")                         # first entry
        first = self.buffer().split("\n")[0].strip()
        self.type(ENTER, 0.8)
        self.assertEqual(self.s.text().split("\n")[0].rstrip(), PROMPT.rstrip() + " " + first)

    def test_enter_without_selection_runs_the_line_as_is(self):
        self.type("cat f\t")                    # menu open, nothing selected
        self.type(ENTER, 0.6)
        lines = self.s.text().split("\n")
        self.assertEqual(lines[0].rstrip(), PROMPT.rstrip() + " cat f")
        self.assertIn("No such file", lines[1])

    def test_shift_tab_goes_back(self):
        self.type("cat f\t\t\t")
        self.type("\x1b[Z")
        self.assertTrue(self.buffer().split("\n")[0].startswith("cat fa1.txt"))

    def test_escape_restores_original(self):
        self.type("cat f\t\t"); self.type("\x1b", 0.4)
        self.assertEqual(self.buffer(), "cat f")
        self.assertEqual(self.s.text().count("\n"), 0)

    def test_typing_closes_menu(self):
        self.type("cat f\t"); self.type("a")
        self.assertEqual(self.s.text().count("\n"), 0)
        self.assertEqual(self.buffer(), "cat fa")

    def test_spaces_in_names_are_escaped(self):
        self.type("cat has\t")
        self.assertEqual(self.buffer(), "cat has\\ space.txt")

    def test_command_completion_has_man_description(self):
        self.type("gre\t", 2.0)
        self.assertRegex(self.s.text(), r"grep\s+print lines")

    def test_builtin_description(self):
        self.type("ki\t", 2.0)
        self.assertIn("signal a process", self.s.text())

    def test_python_attribute_completion_has_types(self):
        self.run_cmd("import os")
        self.type("os.pathse\t")
        self.assertEqual(self.buffer(), "os.pathsep")

    def test_python_attribute_menu(self):
        self.run_cmd("import os")
        self.type("os.path.jo\t")
        self.assertEqual(self.buffer(), "os.path.join")
        self.type(CTRL("u") + "os.pa\t")
        self.assertRegex(self.s.text(), r"pardir\s+str")

    def test_flag_completion_from_help(self):
        self.type("ls --almost\t")
        self.assertEqual(self.buffer(), "ls --almost-all")
        self.type(CTRL("u") + "ls --al\t")
        self.assertRegex(self.s.text(), r"--all\s+do not ignore")

    def test_env_var_completion(self):
        self.type("echo $HOM\t")
        self.assertEqual(self.buffer(), "echo $HOME")

    def test_menu_below_wrapped_line(self):
        self.type("echo " + "w" * 50 + " f\t")
        self.assertIn("fa1.txt", self.s.text())
        self.type("\x1b", 0.4)
        self.assertEqual(self.buffer(), "echo " + "w" * 50 + " f")
        self.assertEqual(len([l for l in self.s.screen.lines() if l.strip()]), 2)

    def test_menu_taller_than_screen_scrolls(self):
        for i in range(40):
            with open(os.path.join(self.s.home, f"many_{i:02}"), "w"): pass
        self.type("cat many_\t")
        self.assertLessEqual(self.s.screen.r, self.rows - 1)
        self.type("\t" * 30, 0.4)
        self.assertTrue(self.buffer().split("\n")[0].startswith("cat many_"))
        self.type("\x1b", 0.4)
        self.assertEqual(self.buffer(), "cat many_")


class WideMenu(EditorCase):
    """Menu rows must be measured in display columns, not code points, or exiting leaves rows behind."""
    rows, cols = 14, 40

    def leftovers(self):
        return [l for l in self.s.screen.lines() if l.strip() and not l.startswith(("tester@", "f"))]

    def prepare(self, names):
        for n in names:
            with open(os.path.join(self.s.home, n), "w"): pass
        for _ in range(10): self.type("echo f" + ENTER, 0.05)
        self.s.pump(0.3)

    def test_wide_characters_clear_after_escape(self):
        self.prepare(["漢字ファイル_%02d" % i for i in range(12)] + ["😀emoji_%02d" % i for i in range(6)])
        self.type("ls \t", 0.8)
        self.assertTrue(self.leftovers())                          # the menu is showing
        self.type("\x1b", 0.6)
        self.assertEqual(self.leftovers(), [])

    def test_wide_characters_clear_after_enter_and_ctrl_c(self):
        self.prepare(["漢字ファイル_%02d" % i for i in range(12)])
        for key in ("\x03", ENTER):
            self.type("echo " + "ls \t"[3:], 0.1) if False else self.type("ls \t", 0.8)
            self.type(key, 0.6)
            self.type(CTRL("u"), 0.2)
            self.assertFalse([l for l in self.leftovers() if "漢字" in l and "ls" not in l and "0B" in l])

    def test_long_names_and_control_characters(self):
        self.prepare(["long_%02d_" % i + "x" * 60 for i in range(6)] + ["tab\there", "ctl\x01char"])
        self.type("ls \t", 0.8)
        self.type("\x1b", 0.6)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.buffer(), "ls")


class Highlighting(EditorCase):
    def test_program_names_are_not_coloured(self):
        self.type("zzzznotacmd")
        raw = self.s.raw.decode()
        self.assertNotIn("\x1b[31mzzzznotacmd", raw)            # neither "unknown" red…
        self.type(CTRL("u") + "ls -l")
        raw = self.s.raw.decode()
        self.assertNotIn("\x1b[34mls", raw)                       # …nor "known" blue

    def test_numbers_match_the_prompts_directory_colour(self):
        self.type("len([1])+50")
        raw = self.s.raw.decode()
        self.assertIn("\x1b[36m50", raw)                         # same colour as the cwd in the prompt (\x1b[36m)
        self.assertNotIn("\x1b[35m", raw)

    def test_strings_yellow_operators_cyan(self):
        self.type("echo 'a b' | cat")
        raw = self.s.raw.decode()
        self.assertIn("\x1b[33m'a b'", raw)
        self.assertIn("\x1b[36m|", raw)

    def test_existing_path_underlined(self):
        self.type("ls /usr")
        self.assertIn("\x1b[4m/usr", self.s.raw.decode())


class Prompt(EditorCase):
    def test_git_branch_in_white(self):
        os.makedirs(os.path.join(self.s.home, "proj", ".git"))
        with open(os.path.join(self.s.home, "proj", ".git", "HEAD"), "w") as f: f.write("ref: refs/heads/feature/x\n")
        self.run_cmd("cd proj")
        self.assertIn("(feature/x)>", self.s.text())
        self.assertIn("\x1b[37m(feature/x)", self.s.raw.decode())

    def test_no_branch_outside_repo(self):
        self.assertNotIn("(", self.s.text())

    def test_exit_status_in_prompt(self):
        self.run_cmd("false")
        self.assertIn("[1]>", self.s.text())

    def test_full_path_is_shown(self):
        os.makedirs(os.path.join(self.s.home, "alpha", "beta", "gamma"))
        self.run_cmd("cd alpha/beta/gamma")
        self.assertIn("~/alpha/beta/gamma>", self.s.text())
        self.assertNotIn("~/a/b/gamma", self.s.text())


class DefaultAliases(EditorCase):
    def test_ls_is_colored_when_supported(self):
        import subprocess
        supported = subprocess.run(["ls", "--color=auto", "-d", "."], capture_output=True).returncode == 0
        self.run_cmd("alias")
        self.assertEqual("alias ls='ls --color=auto'" in self.s.text(), supported)

    def test_ls_output_is_colored_on_a_tty_but_not_in_a_pipe(self):
        os.makedirs(os.path.join(self.s.home, "adir"))
        self.s.raw = b""
        self.run_cmd("ls -d adir")
        self.assertIn("\x1b[01;34madir", self.s.raw.decode())          # directories are blue on a terminal
        self.s.raw = b""
        self.run_cmd("ls -d adir | cat")
        self.assertNotIn("\x1b[01;34m", self.s.raw.decode().split("ls -d adir")[-1])

    def test_rc_file_can_override(self):
        with open(os.path.join(self.s.home, ".pyshrc"), "w") as f: f.write("aliases['ls'] = 'ls -1'\n")
        s2 = Session(rows=24, cols=60, home=self.s.home); self.addCleanup(s2.close)
        s2.send("alias\r", 0.5)
        self.assertIn("alias ls='ls -1'", s2.text())


class ImportCwdSetting(unittest.TestCase):
    """`import_cwd` in ~/.pyshrc controls whether the current directory is searched by `import`."""
    def session(self, rc):
        home = tempfile.mkdtemp(prefix="pysh-test-")
        with open(os.path.join(home, "mymod.py"), "w") as f: f.write("x = 42\n")
        if rc is not None:
            with open(os.path.join(home, ".pyshrc"), "w") as f: f.write(rc)
        s = Session(rows=24, cols=80, home=home); self.addCleanup(s.close)
        return s

    def test_default_imports_from_the_cwd(self):
        s = self.session(None)
        s.send("import mymod\r", 0.6); s.send("print(mymod.x + 1)\r", 0.6)
        self.assertIn("\n43", s.text())

    def test_can_be_turned_off_in_the_rc_file(self):
        s = self.session("import_cwd = False\n")
        s.send("import mymod\r", 0.6)
        self.assertIn("No module named 'mymod'", s.text())


class RcFile(unittest.TestCase):
    """~/.pyshrc runs like a script at interactive startup."""
    def session(self, rc, files=()):
        home = tempfile.mkdtemp(prefix="pysh-test-")
        for name, text in ((".pyshrc", rc), *files):
            if text is not None:
                with open(os.path.join(home, name), "w") as f: f.write(text)
        s = Session(rows=40, cols=100, home=home); self.addCleanup(s.close)
        return s

    def ask(self, s, command):
        s.send(command + "\r", 0.8)
        return s.text()

    def test_exports_aliases_substitution_and_python(self):
        s = self.session("export EDITOR=vim\nexport GREETING=\"hi $USER\"\nexport N=$(echo 42)\nalias hello='echo HELLO'\n"
                         "import os\nif True:\n    os.environ['FROM_BLOCK'] = 'b'\n\ndef double(x): return x * 2\n")
        self.assertIn("E=vim G=hi tester N=42 B=b", self.ask(s, 'sh -c \'echo "E=$EDITOR G=$GREETING N=$N B=$FROM_BLOCK"\''))
        self.assertIn("\nHELLO", self.ask(s, "hello"))
        self.assertIn("\n14", self.ask(s, "double(7)"))

    SCRIPT_STYLE = r'''#!/usr/local/bin/pysh
"""My startup file - a docstring spanning
several lines, like the examples have."""
import os, re, sys
import collections as coll
from functools import partial

COLOURS = {
    "ok": 32,
    "bad": 31,
}
square = lambda x: x * x
add_ten = partial(lambda a, b: a + b, 10)

def clean(s):
    """a helper with a docstring"""
    return re.sub(r"[\x00-\x1f]", "?", str(s))

def words(*args):
    r = !(echo @(list(args)))
    return r.out.split()

def shout(text):
    return $(echo @(text)).upper()

class Counter:
    def __init__(self): self.n = 0

    def bump(self):
        self.n += 1
        return self.n

def decorate(f):
    return lambda: "<" + f() + ">"

@decorate
def tag():
    return "tagged"

counter = Counter()
try:
    import no_such_module_xyz
except ImportError:
    missing = True
for key in COLOURS:
    counter.bump()
today = $(echo 2026)
alias hello='echo HELLO'
export FROM_RC=yes
'''

    def test_everything_it_defines_is_available_in_the_session(self):
        s = self.session(self.SCRIPT_STYLE)
        self.assertNotIn("Error", s.text(), s.text())                      # the whole file loaded cleanly
        checks = [
            ("print(os.path.basename('/a/imported'), sys.maxsize > 0, coll.Counter('aab')['a'])", "imported True 2"),
            ("print(square(9), add_ten(5), COLOURS['bad'])", "81 15 31"),
            ("print(clean('a\\tb'), words('x', 'y z'))", "a?b ['x', 'y', 'z']"),
            ("print(shout('quiet'), tag())", "QUIET <tagged>"),
            ("print(counter.bump(), Counter().bump(), missing, today)", "3 1 True 2026"),
            ("hello", "HELLO"),
            ("sh -c 'echo rc=$FROM_RC'", "rc=yes"),
        ]
        for command, expected in checks:
            with self.subTest(command=command):
                s.send("clear\r", 0.4)
                self.assertIn("\n" + expected, self.ask(s, command))

    def test_functions_can_change_globals_set_there(self):
        s = self.session("count = 0\ndef bump():\n    global count\n    count += 1\n")
        self.ask(s, "bump(); bump()")
        self.assertIn("\n2", self.ask(s, "count"))

    def test_file_name_is_available_while_it_runs(self):
        s = self.session("import os\nRC_NAME = os.path.basename(__file__)\n")
        self.assertIn("\n'.pyshrc'", self.ask(s, "RC_NAME"))

    def test_path_with_tilde(self):
        s = self.session("export PATH=~/bin:$PATH\n")
        self.assertIn(f"first={s.home}/bin\n", self.ask(s, 'sh -c \'echo "first=${PATH%%:*}"\'') + "\n")

    def test_errors_do_not_stop_the_file_or_mark_the_prompt(self):
        s = self.session("no_such_command_here\n1/0\nfalse\nexport AFTER=yes\n")
        self.assertIn("command not found", s.text())
        self.assertIn("ZeroDivisionError", s.text())
        self.assertNotIn("]>", s.text())
        self.assertIn("A=yes", self.ask(s, "sh -c 'echo A=$AFTER'"))

    def test_does_not_import_from_the_current_directory(self):
        # the session starts in $HOME, which holds a planted module
        s = self.session("import planted_module\n", files=(("planted_module.py", "print('PLANTED RAN')\n"),))
        self.assertIn("No module named 'planted_module'", s.text())
        self.assertNotIn("PLANTED RAN\n", s.text())
        self.assertIn("PLANTED RAN", self.ask(s, "import planted_module"))        # but typing it yourself still works

    def test_scripts_do_not_read_it(self):
        import subprocess
        from term import PYSH
        s = self.session("export ONLY_INTERACTIVE=1\n")
        r = subprocess.run([PYSH, "-c", "sh -c 'echo v=$ONLY_INTERACTIVE'"], capture_output=True, text=True,
                           env={"HOME": s.home, "PATH": os.environ["PATH"]})
        self.assertEqual(r.stdout.strip(), "v=")


class DefaultRcFile(unittest.TestCase):
    """An interactive pysh with no ~/.pyshrc writes a starter one and runs it."""
    def home(self):
        d = tempfile.mkdtemp(prefix="pysh-test-")
        self.addCleanup(lambda: (os.chmod(d, 0o700), __import__("shutil").rmtree(d, ignore_errors=True)))
        return d

    def start(self, home):
        s = Session(rows=40, cols=100, home=home); self.addCleanup(s.close)
        return s

    def ask(self, s, command):
        s.send(command + "\r", 0.8)
        return s.text()

    def read(self, path):
        with open(path) as f: return f.read()

    def test_created_with_the_ls_alias_and_documented_settings(self):
        home = self.home(); rc = os.path.join(home, ".pyshrc")
        s = self.start(home)
        self.assertNotIn("Error", s.text(), s.text())
        text = self.read(rc)
        for word in ('aliases["ls"] = "ls --color=auto"', "# probe_flags = True", "# import_cwd = True", "# complete_ignore_dirs"):
            self.assertIn(word, text)
        self.assertIn("alias ls='ls --color=auto'", self.ask(s, "alias"))
        self.assertIn("NameError", self.ask(s, "print(_ls)"))                 # its scratch variable doesn't linger

    def test_the_commented_lines_are_valid_when_uncommented(self):
        home = self.home(); rc = os.path.join(home, ".pyshrc")
        self.start(home).close()
        lines = self.read(rc).split("\n")
        start = lines.index("# Settings, shown with their defaults. Uncomment to change.")
        with open(rc, "w") as f:
            f.write("\n".join(l[2:] if i > start and l.startswith("# ") and not l.startswith("# Examples") else l
                              for i, l in enumerate(lines)))
        s = self.start(home)
        self.assertNotIn("Error", s.text(), s.text())
        self.assertIn("alias ll='ls -l'", self.ask(s, "alias"))

    def test_an_existing_file_is_left_alone(self):
        home = self.home(); rc = os.path.join(home, ".pyshrc")
        with open(rc, "w") as f: f.write("export MINE=1\n")
        s = self.start(home)
        self.assertEqual(self.read(rc), "export MINE=1\n")
        self.assertNotIn("color=auto", self.ask(s, "alias"))                 # the alias lives in the file now

    def test_unwritable_home_still_gets_the_defaults(self):
        if os.geteuid() == 0: self.skipTest("root can write anywhere")
        home = self.home(); os.chmod(home, 0o500)
        s = self.start(home)
        self.assertFalse(os.path.exists(os.path.join(home, ".pyshrc")))
        self.assertIn("alias ls='ls --color=auto'", self.ask(s, "alias"))

    def test_dangling_symlink_is_not_written_through(self):
        home = self.home(); target = os.path.join(home, "elsewhere")
        os.symlink(target, os.path.join(home, ".pyshrc"))
        s = self.start(home)
        self.assertFalse(os.path.exists(target))
        self.assertNotIn("Error", s.text())

    def test_old_python_rc_name_is_no_longer_read(self):
        home = self.home()
        with open(os.path.join(home, ".pyshrc.py"), "w") as f: f.write("aliases['old'] = 'echo old'\n")
        s = self.start(home)
        self.assertNotIn("alias old=", self.ask(s, "alias"))

    def test_starter_file_holds_the_current_prompt(self):
        home = self.home()
        s = self.start(home)
        self.assertIn('PS1 = "{venv}{green}{user}@{host}{reset} {cyan}{cwd}{reset}{white}{branch}{reset}{red}{status}{reset}{grey}>{reset} "',
                      self.read(os.path.join(home, ".pyshrc")))
        raw = s.raw.decode()
        for piece in ("\x1b[32mtester@", "\x1b[36m~", "\x1b[90m>"): self.assertIn(piece, raw)

    def test_scripts_and_dash_c_create_nothing(self):
        import subprocess
        from term import PYSH
        home = self.home()
        subprocess.run([PYSH, "-c", "echo hi"], env={"HOME": home, "PATH": os.environ["PATH"]}, capture_output=True, check=True)
        subprocess.run([PYSH], input="echo hi\n", env={"HOME": home, "PATH": os.environ["PATH"]}, capture_output=True, text=True, check=True)
        self.assertFalse(os.path.exists(os.path.join(home, ".pyshrc")))


class UserPrompt(unittest.TestCase):
    """PS1 (and the older `prompt`) set in ~/.pyshrc or typed at the shell."""
    def session(self, rc):
        home = tempfile.mkdtemp(prefix="pysh-test-")
        with open(os.path.join(home, ".pyshrc"), "w") as f: f.write(rc)
        s = Session(rows=30, cols=80, home=home); self.addCleanup(s.close)
        return s

    def ask(self, s, command):
        s.send(command + "\r", 0.7)
        return s.text()

    def test_fields_and_colours(self):
        s = self.session('PS1 = "{magenta}[{user}]{reset} {cwd} {status}> "\n')
        self.assertIn("[tester] ~ >", s.text())
        self.assertIn("\x1b[35m[tester]", s.raw.decode())
        self.assertIn("[tester] ~ [1]>", self.ask(s, "false"))

    def test_changing_it_in_the_session(self):
        s = self.session("")
        self.assertIn("\nnew> ", self.ask(s, 'PS1 = "new> "') + " ")

    def test_function_with_shell_substitution_is_not_formatted(self):
        s = self.session('def PS1(): return $(echo hey) + " {cwd}> "\n')
        self.assertIn("hey {cwd}>", s.text())

    def test_editing_still_lines_up_with_a_custom_prompt(self):
        s = self.session('PS1 = "{bold}{yellow}py{reset}> "\n')
        s.send("echo ac", 0.4); s.send("\x1b[D", 0.3); s.send("b", 0.4)
        self.assertIn("py> echo abc", s.text())
        self.assertIn("\nabc", self.ask(s, ""))

    def test_multi_line_prompt(self):
        s = self.session('PS1 = "first {cwd}\\n> "\n')
        text = self.ask(s, "echo one")
        self.assertIn("first ~\n> echo one\none\nfirst ~\n>", text)

    def test_branch_value_is_not_treated_as_a_template(self):
        s = self.session('PS1 = "{cwd}> "\n')
        os.makedirs(os.path.join(s.home, "{user}"))
        self.assertIn("~/{user}>", self.ask(s, "cd '{user}'"))

    def test_a_broken_one_falls_back_and_says_so_once(self):
        s = self.session('PS1 = "{nope}> "\n')
        text = self.ask(s, "echo still-works")
        self.assertEqual(text.count("bad PS1"), 1, text)
        self.assertIn("\nstill-works", text)
        self.assertIn("tester@", text)
        self.ask(s, "def PS1(): return 1/0")
        text = self.ask(s, "")                                   # a blank line ends the definition
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("\nalive", self.ask(s, "echo alive"))

    def test_older_prompt_setting_still_wins(self):
        s = self.session('PS1 = "ps1> "\ndef prompt(): return "old> "\n')
        self.assertIn("old>", s.text())


class ForceShellEditing(EditorCase):
    def test_percent_line_runs_as_shell(self):
        self.run_cmd("% export Q=1; sh -c 'echo q=$Q'")
        self.assertIn("\nq=1", self.s.text())

    def test_highlighting_after_percent(self):
        self.type("% ls")
        raw = self.s.raw.decode()
        self.assertIn("\x1b[90m%", raw)
        self.assertNotIn("\x1b[34mls", raw)                       # the program name stays plain

    def test_completion_after_percent_keeps_the_percent(self):
        os.makedirs(os.path.join(self.s.home, "uniq_dir_abc"), exist_ok=True)
        self.type("% cd uniq_d\t")
        self.assertEqual(self.buffer(), "% cd uniq_dir_abc/")

    def test_command_completion_after_percent(self):
        self.type("% ki\t", 2.0)
        self.assertIn("signal a process", self.s.text())
        self.assertTrue(self.buffer().split("\n")[0].startswith("% ki"))


class StreamingInteractive(EditorCase):
    def test_ctrl_c_stops_a_streaming_loop_and_its_command(self):
        self.type("for l in ![sh -c 'while true; do echo tick; sleep 0.2; done']: print(l)" + ENTER, 0.3)
        self.type(ENTER, 1.0)
        self.assertIn("tick", self.s.text())
        self.type(CTRL("c"), 0.8)
        self.assertIn("KeyboardInterrupt", self.s.text())
        self.assertEqual(self.run_cmd("echo back").count("back"), 2)      # prompt works again (command + output)

    def test_live_dollar_bracket_runs_in_foreground(self):
        self.run_cmd("r = $[echo foreground]")
        self.assertIn("\nforeground", self.s.text())


class LongLines(EditorCase):
    cols, rows = 80, 24

    def test_line_longer_than_the_old_8k_limit(self):
        text = "x" * 20000
        self.s.send("printf %s " + text + " | wc -c" + ENTER, 3.0)
        self.s.wait_for("20000", 20)
        self.assertIn("20000", self.s.text().split("\n")[-2] + self.s.text().split("\n")[-1])

    def test_pasted_text_is_inserted_in_one_go(self):
        import time
        t0 = time.time()
        self.s.send("a" * 5000, 1.0)
        self.assertLess(time.time() - t0, 15)
        self.s.send(CTRL("a") + CTRL("k"))                             # editing still works on a long line
        self.assertEqual(self.buffer(), "")

    def test_pasted_text_with_utf8_and_a_control_key(self):
        self.s.send("héllo wörld 漢字" + CTRL("a") + "X", 1.0)
        self.assertEqual(self.buffer(), "Xhéllo wörld 漢字")


class Resize(EditorCase):
    """The terminal is resized while a line is being edited (the emulator behaves like xterm: no reflow)."""
    cols, rows = 60, 20

    def prompts(self, s):
        return [i for i, l in enumerate(s.screen.lines()) if l.startswith("tester@")]

    def logical_line(self, s, start):
        return "".join(s.screen.lines()[start:]).strip()

    def new_session(self, **kw):
        s = Session(rows=20, cols=60, **kw); self.addCleanup(s.close)
        return s

    def test_shrinking_repaints_in_place(self):
        s = self.new_session()
        s.send("echo " + "w" * 70, 0.4)
        s.resize(20, 40)
        self.assertEqual(len(self.prompts(s)), 1)                       # no extra prompt line
        start = self.prompts(s)[0]
        self.assertIn("echo " + "w" * 70, self.logical_line(s, start))
        a = PW + 5 + 70
        self.assertEqual((s.screen.r, s.screen.c), (start + a // 40, a % 40))
        s.send("Z", 0.4)                                                # editing carries on
        self.assertIn("w" * 10 + "Z", self.logical_line(s, start))

    def test_widening_repaints_in_place(self):
        s = self.new_session()
        s.send("echo " + "v" * 70, 0.4)
        s.resize(20, 100)
        self.assertEqual(len(self.prompts(s)), 1)
        start = self.prompts(s)[0]
        self.assertIn("echo " + "v" * 70, self.logical_line(s, start))
        a = PW + 5 + 70
        self.assertEqual((s.screen.r, s.screen.c), (start + a // 100, a % 100))

    def test_dragging_does_not_pile_up_prompts(self):
        s = self.new_session()
        s.send("t", 0.4)
        for width in range(60, 30, -1):                                 # a drag: many events, no pause
            fcntl_resize(s, 20, width)
        s.pump(0.8)
        s.screen.resize(20, 30)
        for width in range(30, 70, 2):
            s.resize(20, width, quiet=0.15)
        self.assertEqual(len(self.prompts(s)), 1, s.text())
        self.assertTrue(s.text().rstrip().endswith("t"))

    def test_cursor_in_the_middle_of_a_wrapped_line(self):
        s = self.new_session()
        s.send("echo " + "m" * 70 + CTRL("a") + RIGHT * 10, 0.5)
        s.resize(20, 40)
        start = self.prompts(s)[0]
        a = PW + 10
        self.assertEqual((s.screen.r, s.screen.c), (start + a // 40, a % 40))

    def test_resize_while_a_menu_is_open(self):
        s = self.new_session()
        for n in ("fa1.txt", "fb2.txt"):
            with open(os.path.join(s.home, n), "w") as f: f.write("x")
        s.send("cat f\t", 0.5)
        s.resize(20, 30)
        self.assertIn("fa1.txt", s.text()); self.assertIn("fb2.txt", s.text())
        s.send("\x1b", 0.5)
        lines = s.screen.lines()
        last_prompt = max(self.prompts(s))
        self.assertEqual([l for l in lines[last_prompt + 1:] if l.strip()], [])     # menu gone below the prompt

    def test_resize_outside_editing_is_harmless(self):
        self.run_cmd("echo before")
        self.s.resize(20, 40)
        self.assertIn("\nafter", self.run_cmd("echo after"))

    def test_terminal_that_cannot_answer_falls_back_to_a_fresh_prompt(self):
        s = self.new_session(answer_dsr=False, env={"PYSH_RESIZE": "redraw"})
        s.send("echo hello", 0.4)
        s.resize(20, 40, quiet=0.8)
        self.assertEqual(len(self.prompts(s)), 2)                        # old one stays; new one below it
        self.assertIn("echo hello", self.logical_line(s, self.prompts(s)[-1]))

    @unittest.skipUnless(__import__("shutil").which("tmux"), "needs tmux (a real terminal that reflows)")
    def test_reflowing_terminal_real_tmux(self):
        import subprocess, tempfile, time
        home = tempfile.mkdtemp(); sock = f"pyshtest{os.getpid()}"
        from term import PYSH
        def tmux(*a): return subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True).stdout
        self.addCleanup(lambda: tmux("kill-server"))
        tmux("new-session", "-d", "-x", "70", "-y", "20", "-c", home, f"env HOME={home} USER=tester {PYSH}")
        for _ in range(60):
            if "tester@" in tmux("capture-pane", "-p"): break
            time.sleep(0.1)
        text = "echo " + "r" * 80
        tmux("send-keys", "-l", text); time.sleep(1.0)
        for w in (65, 60, 55, 50, 45): tmux("resize-window", "-x", str(w), "-y", "20")      # a quick drag
        time.sleep(1.2)
        logical = [l for l in tmux("capture-pane", "-p", "-J").split("\n") if l.strip()]
        self.assertEqual(len([l for l in logical if l.startswith("tester@")]), 1, logical)     # no duplicated prompt
        self.assertTrue(logical[-1].endswith(text), logical)
        cx, cy = map(int, tmux("display-message", "-p", "#{cursor_x} #{cursor_y}").split())
        rows = tmux("capture-pane", "-p").split("\n")
        start = max(i for i, l in enumerate(rows) if l.startswith("tester@"))
        a = PW + len(text)
        self.assertEqual((cx, cy), (a % 45, start + a // 45))                                   # cursor follows the reflow
        for w in (60, 80): tmux("resize-window", "-x", str(w), "-y", "20")                     # and back out again
        time.sleep(1.0)
        logical = [l for l in tmux("capture-pane", "-p", "-J").split("\n") if l.strip()]
        self.assertEqual(len([l for l in logical if l.startswith("tester@")]), 1, logical)
        tmux("send-keys", "Z"); time.sleep(0.5)
        self.assertTrue(tmux("capture-pane", "-p", "-J").rstrip().endswith(text + "Z"))


def fcntl_resize(session, rows, cols):
    """Resize the pty without waiting for the program (one event of a drag)."""
    import fcntl, struct, termios
    fcntl.ioctl(session.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PromptCwd(EditorCase):
    def test_prompt_uses_real_directory_despite_stale_pwd(self):
        s = Session(rows=24, cols=60, env={"PWD": "/mnt/c/Users/elsewhere"}); self.addCleanup(s.close)
        self.assertIn("~>", s.text())                                  # home, not the stale Windows-side path
        self.assertNotIn("mnt", s.text())


class PromptSpace(EditorCase):
    def test_output_without_newline_is_not_overwritten_by_prompt(self):
        self.run_cmd("printf partial")
        lines = self.s.text().split("\n")
        self.assertEqual(lines[1].rstrip(), "partial%")             # marker shows the missing newline
        self.assertTrue(lines[2].startswith("tester@"))

    def test_normal_output_has_no_marker(self):
        self.run_cmd("echo whole")
        self.assertNotIn("%", self.s.text())

    def test_first_prompt_has_no_marker(self):
        self.assertEqual(self.s.text(), PROMPT.rstrip())


if __name__ == "__main__":
    unittest.main()

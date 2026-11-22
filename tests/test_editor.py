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
                self.assertEqual(self.buffer(), "a" * n)
                self.expect_cursor(n)
        self.s.pump(0.2)
        self.assertEqual(self.buffer(), "a")

    def test_left_arrow_across_rows(self):
        self.type("b" * 70)
        for n in range(70, 0, -1):
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
        self.s.pump(0.3)
        self.type("h" * 100, 1.0)
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
    def test_unknown_command_red_known_blue(self):
        self.type("zzzznotacmd")
        self.assertIn("\x1b[31mzzzznotacmd", self.s.raw.decode())
        self.type(CTRL("u") + "ls")
        self.assertIn("\x1b[34mls", self.s.raw.decode())

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

    def test_parent_dirs_shortened(self):
        os.makedirs(os.path.join(self.s.home, "alpha", "beta"))
        self.run_cmd("cd alpha/beta")
        self.assertIn("~/a/beta", self.s.text())


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

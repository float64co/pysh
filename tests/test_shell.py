import os, tempfile, unittest
from helpers import Base, run, out


class Dispatch(Base):
    def test_python_assignment_and_expression(self):
        self.assertOut("x = 6 * 7\nprint(x)", "42")

    def test_expression_is_displayed_in_repl_mode(self):
        self.assertOut("x = 6 * 7\nx\n", "42", mode="stdin")

    def test_command_with_args_is_shell(self):
        self.assertOut("ls -d /", "/")

    def test_bare_command_is_shell(self):
        self.assertEqual(run("true").rc, 0)
        self.assertEqual(run("false").rc, 1)

    def test_python_builtin_name_with_unbound_args_is_shell(self):
        self.assertOut("id -u", str(os.getuid()))

    def test_call_syntax_stays_python(self):
        self.assertOut("print(len('abc'))", "3")

    def test_bound_name_shadows_command(self):
        self.assertOut("ls = 5\nprint(ls)", "5")

    def test_unknown_bare_name_is_command_not_found(self):
        r = run("definitely_not_a_cmd\n", mode="stdin")
        self.assertIn("command not found", r.err)

    def test_multiline_python_block(self):
        self.assertOut("for i in range(3):\n    print(i)\n\nprint('done')", "0\n1\n2\ndone")

    def test_function_definition(self):
        self.assertOut("def f(a):\n    return a + 1\n\nprint(f(1))", "2")

    def test_help_builtin_not_python_help(self):
        self.assertIn("Usage: pysh", out("help"))

    def test_python_help_call_still_works(self):
        self.assertIn("len", out("help(len)"))


class Pipes(Base):
    def test_pipe(self):
        self.assertOut("echo hello | tr a-z A-Z", "HELLO")

    def test_three_stage_pipe(self):
        self.assertOut("printf 'b\\na\\nc\\n' | sort | head -2", "a\nb")

    def test_and_or_list(self):
        self.assertOut("true && echo yes\nfalse || echo recovered\nfalse && echo no\ntrue || echo no", "yes\nrecovered")

    def test_semicolon(self):
        self.assertOut("echo a; echo b", "a\nb")

    def test_exit_status_of_last_stage(self):
        self.assertEqual(run("true | false").rc, 1)
        self.assertEqual(run("false | true").rc, 0)

    def test_command_not_found(self):
        r = run("nonexistent_cmd_xyz arg\n")
        self.assertIn("command not found", r.err)
        self.assertEqual(r.rc, 127)

    def test_sigpipe_does_not_hang(self):
        self.assertOut("yes | head -3", "y\ny\ny")


class Redirects(Base):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def p(self, n): return os.path.join(self.d, n)

    def test_out_and_in(self):
        self.assertOut(f"echo hi > {self.p('a')}\ncat < {self.p('a')}", "hi")

    def test_append(self):
        self.assertOut(f"echo 1 > {self.p('a')}\necho 2 >> {self.p('a')}\ncat {self.p('a')}", "1\n2")

    def test_stderr_to_file(self):
        r = run(f"ls /nonexistent_dir 2> {self.p('e')}\ncat {self.p('e')} | head -c 10")
        self.assertEqual(r.err, "")
        self.assertIn("ls:", r.out)

    def test_stderr_to_stdout(self):
        self.assertIn("ls:", out("ls /nonexistent_dir 2>&1 | head -c 20"))

    def test_both(self):
        r = run(f"ls /nonexistent_dir &> {self.p('b')}\ncat {self.p('b')} | head -c 3")
        self.assertEqual(r.err, "")
        self.assertEqual(r.out, "ls:")

    def test_stdout_to_stderr(self):
        r = run("echo oops >&2")
        self.assertEqual((r.out, r.err.strip()), ("", "oops"))

    def test_missing_input_file(self):
        r = run("cat < /nonexistent_file_xyz\n")
        self.assertNotEqual(r.rc, 0)


class Expansion(Base):
    def test_single_quotes_literal(self):
        self.assertOut("export V=x\necho '$V'", "$V")

    def test_double_quotes_expand(self):
        self.assertOut("export V=x\necho \"[$V]\"", "[x]")

    def test_unquoted_var_splits_but_quoted_does_not(self):
        self.assertOut("export V='a b'\nprintf '<%s>' $V\necho\nprintf '<%s>' \"$V\"", "<a><b>\n<a b>")

    def test_tilde(self):
        self.assertOut("echo ~", out("echo $HOME"))

    def test_glob(self):
        d = tempfile.mkdtemp()
        for n in ("a.txt", "b.txt", "c.md"): open(os.path.join(d, n), "w").close()
        self.assertOut(f"cd {d}\necho *.txt", "a.txt b.txt")

    def test_unmatched_glob_stays_literal(self):
        self.assertOut("echo /nonexistent_dir_xyz/*.zzz", "/nonexistent_dir_xyz/*.zzz")

    def test_quoted_glob_not_expanded(self):
        self.assertOut("echo '*'", "*")

    def test_backslash_escape(self):
        self.assertOut(r"echo a\ b", "a b")

    def test_command_substitution(self):
        self.assertOut("echo $(echo inner)", "inner")

    def test_command_substitution_in_double_quotes(self):
        self.assertOut('echo "[$(echo a b)]"', "[a b]")

    def test_nested_quotes_in_substitution(self):
        self.assertOut('echo "$(echo "a b")"', "a b")

    def test_dollar_question(self):
        self.assertOut("false\necho $?", "1")

    def test_env_prefix(self):
        self.assertOut("FOO=bar sh -c 'echo $FOO'", "bar")

    def test_export_and_unset(self):
        self.assertOut("export A=1\nsh -c 'echo [$A]'\nunset A\nsh -c 'echo [$A]'", "[1]\n[]")

    def test_python_splice(self):
        self.assertOut("xs = ['a', 'b c']\nprintf '<%s>' @(xs)", "<a><b c>")

    def test_comment(self):
        self.assertOut("echo a # not this", "a")


class PythonBridge(Base):
    def test_dollar_paren_returns_str(self):
        self.assertOut("x = $(echo hi)\nprint(repr(x))", "'hi'")

    def test_bang_paren_returns_result(self):
        self.assertOut("r = !(sh -c 'echo out; echo err >&2; exit 3')\nprint(r.returncode, r.out.strip(), r.err.strip())", "3 out err")

    def test_result_truthiness(self):
        self.assertOut("print(bool(!(true)), bool(!(false)))", "True False")

    def test_last(self):
        self.assertOut("false\nprint(last.returncode)", "1")

    def test_env_get_and_set(self):
        self.assertOut("$FOO = 'bar'\nprint($FOO)\nsh -c 'echo $FOO'", "bar\nbar")

    def test_missing_env_is_empty(self):
        self.assertOut("print(repr($NOPE_NOT_SET))", "''")

    def test_dollar_in_python_string_untouched(self):
        self.assertOut("print('$(not run) $X')", "$(not run) $X")

    def test_aliases_string(self):
        self.assertOut("aliases['ll'] = 'echo long'\nll", "long")

    def test_aliases_callable_returns_string(self):
        self.assertOut("aliases['hi'] = lambda a: 'hi ' + ' '.join(a)\nhi there", "hi there")

    def test_aliases_callable_exit_status(self):
        self.assertEqual(run("aliases['f'] = lambda a: 3\nf\n").rc, 3)

    def test_alias_in_pipeline_reads_stdin(self):
        self.assertOut("import sys\naliases['up'] = lambda a: sys.stdin.read().upper()\necho abc | up", "ABC")

    def test_alias_in_pipeline_feeds_next_stage(self):
        self.assertOut("aliases['two'] = lambda a: 'x\\ny'\ntwo | wc -l", "2")

    def test_builtin_in_pipeline(self):
        self.assertOut("type ls | head -c 2", "ls")

    def test_cd_builtin(self):
        self.assertOut("cd /usr\npwd", "/usr")

    def test_cd_dash(self):
        self.assertOut("cd /usr\ncd /\ncd -\npwd", "/usr")

    def test_exit_status(self):
        self.assertEqual(run("exit 7").rc, 7)

    def test_syntax_error_in_command_reports(self):
        r = run("echo 'unterminated\n", mode="stdin")
        self.assertIn("syntax error", r.err)


class Cli(Base):
    @staticmethod
    def pysh(*args):
        import subprocess
        from helpers import PYSH
        return subprocess.run([PYSH, *args], capture_output=True, text=True, timeout=30)

    def test_help(self):
        self.assertIn("Usage: pysh", self.pysh("--help").stdout)

    def test_dash_h(self):
        self.assertIn("Usage: pysh", self.pysh("-h").stdout)

    def test_help_matches_help_builtin(self):
        self.assertEqual(self.pysh("--help").stdout, run("help").out)

    def test_version(self):
        o = self.pysh("--version").stdout
        self.assertTrue(o.startswith("pysh "))
        self.assertIn("Python 3.", o)

    def test_unknown_option(self):
        r = self.pysh("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option", r.stderr)

    def test_c_with_args(self):
        self.assertEqual(run("import sys; print(sys.argv[1:])", "a", "b", mode="c").out.strip(), "['a', 'b']")

    def test_script_args(self):
        self.assertEqual(run("import sys\nprint(sys.argv[1:])", "x", "y").out.strip(), "['x', 'y']")

    def test_stdin_mode(self):
        self.assertOut("echo from-stdin\n", "from-stdin", mode="stdin")

    def test_missing_script(self):
        self.assertEqual(self.pysh("/nonexistent_script").returncode, 127)


if __name__ == "__main__":
    unittest.main()


class ScriptBlocks(Base):
    """Block structure in script files / -c: where a Python block ends and shell lines resume."""

    def test_shell_line_after_block_is_a_command(self):
        self.assertOut("for i in range(2):\n    print(i)\n\necho shell-after", "0\n1\nshell-after")

    def test_shell_line_right_after_block_without_blank(self):
        self.assertOut("for i in range(2):\n    print(i)\necho shell-after", "0\n1\nshell-after")

    def test_blank_lines_inside_a_block(self):
        self.assertOut("def f():\n    a = 1\n\n    return a\n\nprint(f())", "1")

    def test_try_except_finally(self):
        self.assertOut("try:\n    1/0\nexcept ZeroDivisionError:\n    print('caught')\nfinally:\n    print('fin')\necho after", "caught\nfin\nafter")

    def test_if_else_chain(self):
        self.assertOut("x = 2\nif x == 1:\n    print('one')\nelif x == 2:\n    print('two')\nelse:\n    print('other')\necho done", "two\ndone")

    def test_decorator(self):
        self.assertOut("def deco(f):\n    return lambda: 'wrapped ' + f()\n@deco\ndef g():\n    return 'g'\nprint(g())", "wrapped g")

    def test_multiline_brackets_and_strings(self):
        self.assertOut("xs = [1,\n      2,\n      3]\ns = '''a\nb'''\nprint(len(xs), s.splitlines())", "3 ['a', 'b']")

    def test_backslash_continuation_in_shell_line(self):
        self.assertOut("echo one \\\n two", "one two")

    def test_python_then_shell_then_python(self):
        self.assertOut("x = $(echo 5)\nls -d / > /dev/null\nprint(int(x) + 1)", "6")

    def test_class_definition(self):
        self.assertOut("class A:\n    def hi(self):\n        return 'hi'\n\nprint(A().hi())\necho end", "hi\nend")

    def test_heredoc_inside_script_between_blocks(self):
        self.assertOut("for i in range(1):\n    print('p')\ncat <<EOF\nbody\nEOF\nprint('q')", "p\nbody\nq")

    def test_uncaught_exception_ends_a_script(self):
        r = run("raise ValueError('boom')\necho not-reached")
        self.assertIn("boom", r.err)
        self.assertNotIn("not-reached", r.out)
        self.assertEqual(r.rc, 1)

    def test_caught_exception_continues(self):
        self.assertOut("try:\n    raise ValueError('x')\nexcept ValueError:\n    print('handled')\necho after", "handled\nafter")

    def test_failed_command_does_not_end_a_script(self):
        self.assertOut("false\necho still", "still")


class SpliceScope(Base):
    def test_splice_sees_function_locals(self):
        self.assertOut("def f(n):\n    return $(echo @(n * 2))\nprint(f(21))", "42")

    def test_bang_paren_in_function_with_locals(self):
        self.assertOut("def f(n):\n    return !(sh -c @(f'exit {n}')).returncode\nprint(f(3))", "3")

    def test_splice_in_comprehension_and_threads(self):
        self.assertOut("import concurrent.futures as cf\ndef p(n): return $(echo @(n))\nwith cf.ThreadPoolExecutor(3) as ex: print(sorted(ex.map(p, range(5))))",
                       "['0', '1', '2', '3', '4']")


class ShebangScripts(Base):
    """Executable files that start with #!/path/to/pysh."""

    @staticmethod
    def script(body, shebang=None, name="s"):
        import stat, tempfile
        from helpers import PYSH
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w") as f: f.write((shebang or f"#!{PYSH}") + "\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    @staticmethod
    def execute(path, *args, **kw):
        import subprocess
        return subprocess.run([path, *args], capture_output=True, text=True, timeout=30, **kw)

    def test_runs_directly(self):
        r = self.execute(self.script("echo hi\nprint(6 * 7)"))
        self.assertEqual(r.stdout, "hi\n42\n")

    def test_arguments_and_file(self):
        p = self.script("import sys\nprint(sys.argv[1:], __file__ == sys.argv[0])")
        self.assertEqual(self.execute(p, "a", "b c").stdout.strip(), "['a', 'b c'] True")

    def test_exit_status(self):
        self.assertEqual(self.execute(self.script("exit 7")).returncode, 7)
        self.assertEqual(self.execute(self.script("echo ok")).returncode, 0)
        self.assertEqual(self.execute(self.script("false")).returncode, 1)

    def test_env_shebang(self):
        import os, tempfile
        from helpers import PYSH
        bindir = tempfile.mkdtemp()
        os.symlink(PYSH, os.path.join(bindir, "pysh"))
        p = self.script("print('via env')", shebang="#!/usr/bin/env pysh")
        r = self.execute(p, env=dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"]))
        self.assertEqual(r.stdout.strip(), "via env")

    def test_shebang_with_option(self):
        p = self.script("echo one\nfalse\necho two", shebang="#!%s -e" % os.environ.get("PYSH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pysh")))
        r = self.execute(p)
        self.assertEqual((r.stdout, r.returncode), ("one\n", 1))

    def test_traceback_names_the_file_and_line(self):
        p = self.script("x = 1\n\ny = 2\nraise RuntimeError('here')\n")
        r = self.execute(p)
        self.assertIn(f'File "{p}", line 5', r.stderr)             # line 1 is the shebang
        self.assertIn("raise RuntimeError('here')", r.stderr)      # source line shown

    def test_traceback_line_inside_a_block(self):
        p = self.script("for i in range(2):\n    print(i)\n    if i == 1:\n        raise KeyError('k')\n")
        self.assertIn("line 5, in <module>", self.execute(p).stderr)

    def test_shell_errors_name_the_file_and_line(self):
        p = self.script("echo fine\nnot_a_real_command_xyz\n", name="deploy")
        self.assertIn("deploy:3: not_a_real_command_xyz: command not found", self.execute(p).stderr)

    def test_stdin_passes_through(self):
        import subprocess
        p = self.script("import sys\nprint(sys.stdin.read().upper())\nwc -c < /dev/null")
        r = subprocess.run([p], input="piped in", capture_output=True, text=True)
        self.assertIn("PIPED IN", r.stdout)

    def test_works_in_a_pipeline_as_a_filter(self):
        import subprocess
        p = self.script("import sys\nfor line in sys.stdin: print(line.strip()[::-1])")
        r = subprocess.run(f"printf 'abc\\nxyz\\n' | {p} | tr a-z A-Z", shell=True, capture_output=True, text=True)
        self.assertEqual(r.stdout, "CBA\nZYX\n")

    def test_heredoc_and_blocks_in_a_script_file(self):
        p = self.script("for i in range(2):\n    print(i)\ncat <<EOF\nbody $((1+1))\nEOF\n")
        self.assertEqual(self.execute(p).stdout, "0\n1\nbody 2\n")

    def test_relative_invocation(self):
        import subprocess, os
        p = self.script("print('rel')")
        r = subprocess.run(["./" + os.path.basename(p)], cwd=os.path.dirname(p), capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "rel")

    def test_name_is_main(self):
        self.assertEqual(self.execute(self.script("print(__name__)")).stdout.strip(), "__main__")

    def test_main_guard(self):
        p = self.script("def main():\n    print('in main')\nif __name__ == '__main__':\n    main()\n")
        self.assertEqual(self.execute(p).stdout.strip(), "in main")

    def test_dash_dash_stops_option_parsing(self):
        import subprocess
        from helpers import PYSH
        p = self.script("import sys\nprint(sys.argv[1:])")
        r = subprocess.run([PYSH, "--", p, "-e"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "['-e']")

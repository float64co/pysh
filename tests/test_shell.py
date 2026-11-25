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

    def test_env_prefix_builtin_and_alias(self):
        self.assertOut("FOO=bar export X=$FOO\nsh -c 'echo $X'", "")
        self.assertOut("alias e='sh -c \"echo \\$FOO\"'\nFOO=bar e\nsh -c 'echo [$FOO]'", "bar\n[]")

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


class WorkingDirectory(Base):
    """$PWD must describe the real current directory, whatever the parent process left in the environment."""

    def test_stale_inherited_pwd_is_corrected(self):
        d = tempfile.mkdtemp()
        r = run("echo $PWD\nimport os\nprint(os.getcwd())", cwd=d, env={"PWD": "/mnt/c/Users/somebody"})
        real = os.path.realpath(d)
        self.assertEqual(r.out.split("\n")[:2], [real, real])

    def test_unset_pwd_is_set(self):
        d = tempfile.mkdtemp()
        e = {k: v for k, v in os.environ.items() if k != "PWD"}
        import subprocess
        from helpers import PYSH
        r = subprocess.run([PYSH, "-c", "echo [$PWD]"], cwd=d, env=e, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), f"[{os.path.realpath(d)}]")

    def test_relative_pwd_is_replaced(self):
        d = tempfile.mkdtemp()
        self.assertEqual(out("echo $PWD", cwd=d, env={"PWD": "."}), os.path.realpath(d))

    def test_valid_symlink_pwd_is_kept(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "real")); os.symlink("real", os.path.join(d, "link"))
        link = os.path.join(d, "link")
        self.assertEqual(out("echo $PWD", cwd=link, env={"PWD": link}), link)

    def test_cd_updates_pwd_and_children_see_it(self):
        self.assertOut("cd /usr\necho $PWD\nsh -c 'echo $PWD'\ncd lib\necho $PWD\ncd ..\necho $PWD", "/usr\n/usr\n/usr/lib\n/usr")

    def test_cd_keeps_symlinks_logical(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "real")); os.symlink("real", os.path.join(d, "link"))
        link = os.path.join(d, "link")
        self.assertOut(f"cd {link}\necho $PWD\npwd\npwd -P\ncd ..\necho $PWD",
                       f"{link}\n{link}\n{os.path.realpath(link)}\n{d}")

    def test_cd_dash_returns(self):
        self.assertOut("cd /usr\ncd /tmp\ncd -\necho $PWD\necho $OLDPWD", "/usr\n/tmp")

    def test_cd_failure_leaves_pwd(self):
        r = run("cd /usr\ncd /nonexistent_dir_xyz\necho $PWD")
        self.assertEqual(r.out.strip(), "/usr")
        self.assertIn("No such file", r.err)

    def test_cd_physical_flag(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "real")); os.symlink("real", os.path.join(d, "link"))
        self.assertOut(f"cd -P {d}/link\necho $PWD", os.path.join(os.path.realpath(d), "real"))

    def test_pwd_builtin_not_the_binary(self):
        self.assertIn("builtin", out("type pwd"))

    def test_python_getcwd_follows_cd(self):
        self.assertOut("import os\ncd /usr\nprint(os.getcwd())", "/usr")


class ForceShell(Base):
    """A leading % makes the line a shell command, whatever it looks like as Python."""

    def test_assignment_then_command_on_one_line(self):
        self.assertOut("% export X=5; sh -c 'echo $X'", "5")

    def test_python_looking_line_runs_as_shell(self):
        self.assertOut("% echo 1 + 2", "1 + 2")                  # not evaluated as Python

    def test_without_space(self):
        self.assertOut("%echo hi", "hi")

    def test_bound_name_is_still_a_command(self):
        self.assertOut("ls = 5\n% ls -d /", "/")

    def test_env_prefix_form(self):
        self.assertOut("% FOO=bar sh -c 'echo $FOO'", "bar")

    def test_in_repl_mode(self):
        self.assertOut("% echo from-repl\n", "from-repl", mode="stdin")

    def test_inside_a_python_block_line_is_not_special(self):
        self.assertOut("x = 7\nprint(x % 4)", "3")               # modulo untouched


class Streaming(Base):
    def test_lines_arrive_as_they_are_produced(self):
        r = run("import time\nt0 = time.time()\n"
                "for line in ![sh -c 'echo a; sleep 0.6; echo b']:\n    print(line, round(time.time() - t0, 1) < 0.5)\n")
        self.assertEqual(r.out.split("\n")[:2], ["a True", "b False"])    # 'a' was seen before the sleep ended

    def test_iteration_yields_lines_without_newlines(self):
        self.assertOut("print(list(![printf 'x\\ny\\nz\\n']))", "['x', 'y', 'z']")

    def test_pipeline(self):
        self.assertOut("print(list(![printf '1\\n2\\n3\\n' | tail -2]))", "['2', '3']")

    def test_returncode_after_iteration(self):
        self.assertOut("p = ![sh -c 'echo x; exit 3']\nlist(p)\nprint(p.returncode, p.wait())", "3 3")

    def test_result_property(self):
        self.assertOut("p = ![true]\np.wait()\nprint(p.result.returncode, bool(p.result))", "0 True")

    def test_context_manager_terminates_a_running_command(self):
        self.assertOut("import time\nwith ![sh -c 'echo first; sleep 30'] as p:\n    print(p.readline().strip())\nprint(p.poll() is not None)", "first\nTrue")

    def test_readline_and_read(self):
        self.assertOut("p = ![printf 'a\\nb\\nc\\n']\nprint(repr(p.readline()), repr(p.read()))", "'a\\n' 'b\\nc\\n'")

    def test_stderr_stays_on_terminal_by_default(self):
        r = run("print(list(![sh -c 'echo o; echo e >&2']))")
        self.assertEqual((r.out.strip(), r.err.strip()), ("['o']", "e"))

    def test_merge_stderr(self):
        self.assertOut("print(sorted(sh(\"sh -c 'echo o; echo e >&2'\", stream=True, merge=True)))", "['e', 'o']")

    def test_sees_function_locals(self):
        self.assertOut("def f(n): return list(![seq @(n)])\nprint(f(3))", "['1', '2', '3']")

    def test_kill(self):
        self.assertOut("p = ![sleep 30]\np.kill()\nprint(p.wait())", "137")

    def test_timeout_on_stream(self):
        self.assertOut("p = sh('sleep 30', stream=True, timeout=0.3)\nprint(p.wait(), p.timed_out)", "143 True")

    def test_big_output_does_not_deadlock(self):
        self.assertOut("print(sum(1 for _ in ![seq 1 200000]))", "200000")

    def test_unknown_command_streams_nothing(self):
        r = run("print(list(![no_such_cmd_xyz]))")
        self.assertIn("command not found", r.err)
        self.assertIn("[]", r.out)

    def test_lists_are_rejected(self):
        r = run("![echo a; echo b]")
        self.assertIn("one pipeline", r.err)

    def test_repr(self):
        self.assertIn("<Process", out("p = ![true]\nprint(repr(p))"))


class ShFunction(Base):
    def test_default_captures(self):
        self.assertOut("r = sh(\"sh -c 'echo o; echo e >&2'\")\nprint(repr(r.out), repr(r.err))", "'o\\n' 'e\\n'")

    def test_live_not_captured(self):
        r = run("r = sh('echo shown', capture=False)\nprint(repr(r.out), r.returncode)")
        self.assertEqual(r.out.split("\n"), ["shown", "None 0", ""])

    def test_dollar_bracket_is_live(self):
        r = run("r = $[echo live]\nprint(r.returncode, r.out)")
        self.assertEqual(r.out.split("\n")[:2], ["live", "0 None"])

    def test_tee_shows_and_captures(self):
        r = run("r = sh(\"sh -c 'echo o; echo e >&2'\", tee=True)\nprint('got', repr(r.out), repr(r.err))")
        self.assertEqual(r.out.split("\n")[:2], ["o", "got 'o\\n' 'e\\n'"])
        self.assertEqual(r.err.strip(), "e")                      # stderr was shown live too

    def test_check_raises(self):
        self.assertOut("try:\n    sh('sh -c \"exit 4\"', check=True)\nexcept ShellError as e:\n    print('failed', e.returncode, e.result.cmd)", "failed 4 sh -c \"exit 4\"")

    def test_check_passes_on_success(self):
        self.assertOut("print(sh('true', check=True).returncode)", "0")

    def test_timeout_raises(self):
        self.assertOut("import subprocess\ntry:\n    sh('sleep 30', timeout=0.3)\nexcept subprocess.TimeoutExpired as e:\n    print('timed out', e.timeout)", "timed out 0.3")

    def test_timeout_not_hit(self):
        self.assertOut("print(sh('echo quick', timeout=5).out.strip())", "quick")

    def test_timeout_kills_stubborn_process(self):
        import time
        t = time.time()
        r = run("import subprocess\ntry:\n    sh(\"sh -c 'trap \\\"\\\" TERM; sleep 30'\", timeout=0.3)\nexcept subprocess.TimeoutExpired:\n    print('killed')")
        self.assertEqual(r.out.strip(), "killed")
        self.assertLess(time.time() - t, 10)                      # SIGKILL followed SIGTERM

    def test_timeout_with_pipeline(self):
        self.assertOut("import subprocess\ntry:\n    sh('sleep 30 | cat', timeout=0.3)\nexcept subprocess.TimeoutExpired:\n    print('pipeline timed out')", "pipeline timed out")

    def test_scope_sees_locals(self):
        self.assertOut("def f(n): return sh('echo @(n)').out.strip()\nprint(f(7))", "7")


class ExitMessages(Base):
    """sys.exit("text") prints the text to stderr and exits 1, like Python."""
    def test_string_exit_in_a_script_file(self):
        r = run('import sys\nsys.exit("boom: it broke")\n')
        self.assertEqual((r.rc, r.err.strip()), (1, "boom: it broke"), msg=repr(r))

    def test_string_exit_with_c(self):
        r = run('import sys; sys.exit("via -c")', mode="c")
        self.assertEqual((r.rc, r.err.strip()), (1, "via -c"), msg=repr(r))

    def test_string_exit_from_piped_input(self):
        r = run('import sys\nsys.exit("via stdin")\n', mode="stdin")
        self.assertEqual((r.rc, r.err.strip()), (1, "via stdin"), msg=repr(r))

    def test_numeric_and_none_exit(self):
        self.assertEqual(run("import sys\nsys.exit(3)\n").rc, 3)
        self.assertEqual(run("import sys\nsys.exit()\n").rc, 0)
        self.assertEqual(run("import sys\nsys.exit(None)\n").rc, 0)

    def test_stdout_is_flushed_before_the_message(self):
        r = run('import sys\nprint("first")\nsys.exit("second")\n')
        self.assertEqual(r.out, "first\n"); self.assertEqual(r.err.strip(), "second")


class ImportFromCwd(Base):
    """Modules next to you can be imported, as with `python`; the `import_cwd` switches turn that off."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        os.makedirs(os.path.join(self.d, "sub"))
        for name, text in {"mymod.py": "x = 42\n", "sub/sib.py": "y = 7\n", "sub/uses_sib.py": "import sib\nprint('sibling', sib.y)\n",
                           "sub/uses_cwd.py": "import mymod\nprint('cwd module', mymod.x)\n"}.items():
            with open(os.path.join(self.d, name), "w") as f: f.write(text)

    def test_dash_c_and_stdin_search_the_cwd(self):
        self.assertEqual(run("import mymod\nprint(mymod.x)\n", mode="c", cwd=self.d).out.strip(), "42")
        self.assertEqual(run("import mymod\nprint(mymod.x)\n", mode="stdin", cwd=self.d).out.strip(), "42")

    def test_cwd_entry_follows_cd(self):
        r = run("import os\nos.chdir('sub')\nimport sib\nprint(sib.y)\n", mode="stdin", cwd=self.d)
        self.assertEqual(r.out.strip(), "7", msg=repr(r))

    def test_script_searches_its_own_directory_not_the_cwd(self):
        sub = os.path.join(self.d, "sub")
        import subprocess
        from helpers import PYSH
        ok = subprocess.run([PYSH, os.path.join(sub, "uses_sib.py")], capture_output=True, text=True, cwd=self.d)
        self.assertEqual(ok.stdout.strip(), "sibling 7", msg=ok.stderr)
        bad = subprocess.run([PYSH, os.path.join(sub, "uses_cwd.py")], capture_output=True, text=True, cwd=self.d)
        self.assertIn("No module named 'mymod'", bad.stderr)
        self.assertEqual(bad.returncode, 1)

    def test_environment_switch(self):
        r = run("import mymod\n", mode="c", cwd=self.d, env={"PYSH_IMPORT_CWD": "0"})
        self.assertEqual(r.rc, 1); self.assertIn("No module named 'mymod'", r.err)

    def test_import_cwd_is_a_documented_setting(self):
        self.assertEqual(run("print(import_cwd)\n", mode="stdin").out.strip(), "True")


class AssignmentExpansion(Base):
    """`NAME=value` expands as in bash: no word splitting or globbing, `~` after `=` and `:`."""
    def check(self, script, expected):
        r = run(script, mode="stdin")
        self.assertEqual(r.out.strip(), expected, msg=repr(r))

    def test_export_does_not_split_variables(self):
        self.check('export FLAGS="-a -b"\nexport OPTS=$FLAGS\nsh -c \'echo "[$OPTS]"\'\n', "[-a -b]")

    def test_prefix_assignment_does_not_split(self):
        self.check('export F="x  y"\nQ=$F sh -c \'echo "[$Q]"\'\n', "[x  y]")
        self.check('W=$(printf "a  b") sh -c \'echo "[$W]"\'\n', "[a  b]")

    def test_tilde_after_equals_and_colon(self):
        home = tempfile.gettempdir()
        self.check("export P=~/bin:~/x:/usr/bin\nsh -c 'echo $P'\n", f"{home}/bin:{home}/x:/usr/bin")

    def test_no_globbing_or_braces_and_empty_value(self):
        self.check("export G=*.zz{a,b}\nexport E=\nsh -c 'echo \"$G [$E]\"'\n", "*.zz{a,b} []")

    def test_ordinary_arguments_are_unchanged(self):
        self.check('export V="a b"\nprintf "<%s>" $V a=~/z\n', "<a><b><a=~/z>")


class AliasPositions(Base):
    """String aliases expand wherever a command starts, not only at the beginning of the line."""
    SETUP = "alias hi='echo HI'\n"

    def lines(self, script):
        r = run(self.SETUP + script + "\n", mode="stdin")
        return r.out.split("\n")[:-1], r.err

    def check(self, script, *expected):
        out, err = self.lines(script)
        self.assertEqual((out, err), (list(expected), ""), msg=script)

    def test_after_each_separator(self):
        self.check("true; hi", "HI")
        self.check("true && hi", "HI")
        self.check("false || hi", "HI")
        self.check("echo x | hi", "HI")
        self.check("echo x |& hi", "HI")
        self.check("hi & wait", "HI")
        self.check("hi; hi && hi", "HI", "HI", "HI")

    def test_arguments_follow_the_alias(self):
        self.check("true && hi there", "HI there")

    def test_after_assignments_and_redirections(self):
        self.check("true; X=1 hi", "HI")
        self.check("true; </dev/null hi", "HI")

    def test_arguments_and_quoted_text_are_left_alone(self):
        self.check("echo hi", "hi")
        self.check("echo 'a; hi' \"b && hi\"", "a; hi b && hi")
        self.check("echo a\\; hi", "a; hi")
        self.check("echo > hi; cat hi; rm hi", "")                     # a redirect target is not a command

    def test_quoting_the_name_skips_the_alias(self):
        out, err = self.lines("true; \\hi")
        self.assertIn("hi: command not found", err)
        out, err = self.lines("true; 'hi'")
        self.assertIn("hi: command not found", err)

    def test_self_reference_expands_once(self):
        self.check("alias echo='echo pre'\ntrue && echo x; echo y", "pre x", "pre y")

    def test_mutual_reference_terminates(self):
        out, err = self.lines("alias a=b\nalias b=a\ntrue; a")
        self.assertIn("a: command not found", err)

    def test_alias_of_an_alias_and_of_several_commands(self):
        self.check("alias yo=hi\nalias both='hi; yo'\ntrue && both", "HI", "HI")
        self.check("alias count='printf x | wc -c'\ntrue; count", "1")

    def test_in_command_substitution_and_scripts(self):
        self.check("x = $(true; hi)\nprint(x)", "HI")
        r = run("alias hi='echo HI'\ntrue && hi\n")
        self.assertEqual(r.out, "HI\n", msg=repr(r))

    def test_trailing_blank_expands_the_next_word_too(self):
        self.check("alias say='echo '\nsay hi", "echo HI")             # "hi" is expanded because `say` ends in a blank
        self.check("alias say='echo'\nsay hi", "hi")                   # ...and not without it
        self.check("alias run='env '\ntrue && run hi", "HI")            # the sudo case, with env
        self.check("alias run='env\t'\nrun hi", "HI")

    def test_trailing_blank_reaches_only_the_next_word(self):
        self.check("alias say='echo '\nsay x hi", "x hi")
        self.check("alias say='echo '\nsay 'hi'", "hi")                # quoted: left alone
        self.check("alias say='echo '\nsay x; echo hi", "x", "hi")

    def test_trailing_blank_chains_and_terminates(self):
        self.check("alias run='env '\nrun run hi", "HI")                # each word typed by you is expanded afresh
        self.check("alias echo='echo '\necho echo z", "echo z")
        self.check("alias a='echo '\nalias b='c '\nalias c='b '\na b", "b")

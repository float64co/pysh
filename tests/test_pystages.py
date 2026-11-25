"""Python in pipelines: `@name` / `@(expr)` stages, `name=@...` capture and assignment, Python as a source."""
import os, shutil, subprocess, tempfile, textwrap, time, unittest

from helpers import PYSH
from term import Session

CTRL_C, CTRL_Z = "\x03", "\x1a"


class Case(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="pysh-test-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def sh(self, script, *opts, timeout=30):
        path = os.path.join(self.d, "t.pysh")
        with open(path, "w") as f: f.write(textwrap.dedent(script).lstrip("\n"))
        r = subprocess.run([PYSH, *opts, path], capture_output=True, text=True, timeout=timeout, cwd=self.d,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, HOME=self.d))
        return r

    def check(self, script, expected, *opts):
        r = self.sh(script, *opts)
        self.assertEqual((r.stdout, r.stderr), (textwrap.dedent(expected).lstrip("\n"), ""), msg=repr(r))


class Stages(Case):
    def test_function_gets_a_stream(self):
        self.check("""
            import json, csv
            printf '{"a": [1, 2]}' | @(lambda f: json.load(f)["a"])
            printf 'x,y\\n1,2\\n' | @(lambda f: list(csv.reader(f)))
            """, """
            1
            2
            ['x', 'y']
            ['1', '2']
            """)

    def test_output_rules(self):
        self.check("""
            def nothing(f): print("printed:", f.read(), end="")
            def text(f): return "as is"
            def items(f): return [1, "two", "three\\n"]
            def gen(f): return (l.upper() for l in f)
            def other(f): return {"k": 1}
            echo a | @nothing
            echo | @text | od -An -c | tr -d ' '
            echo | @items
            echo b | @gen
            echo | @other
            echo | @(lambda f: range(3))
            """, """
            printed: a
            asis
            1
            two
            three
            B
            {'k': 1}
            range(0, 3)
            """)

    def test_extra_words_are_string_arguments(self):
        self.check("""
            def show(f, *a): print(a, f.read().strip())
            who = "w"
            echo in | @show one "two three" $HOME@ @(who)
            """, f"('one', 'two three', '{self.d}@', 'w') in\n")

    def test_dotted_names_and_expressions(self):
        self.check("""
            import json
            echo '[3]' | @json.load
            echo abc | @(lambda f: f.read()[::-1].strip())
            """, "3\ncba")

    def test_print_goes_to_the_stage_output(self):
        self.check("""
            def p(f): print("inside")
            echo | @p | tr a-z A-Z
            echo | @p > out.txt
            cat out.txt
            """, "INSIDE\ninside\n")

    def test_last_stage_changes_persist_and_middle_stage_changes_do_not(self):
        self.check("""
            seen = []
            def note(f): seen.append(f.read().strip())
            def forward(f): seen.append("middle"); return f.read()
            echo last | @note
            echo x | @forward | cat
            print(seen)
            """, "x\n['last']\n")

    def test_redirections(self):
        self.check("""
            import sys
            printf 'b\\na\\n' | @(lambda f: sorted(f)) > o.txt
            cat o.txt
            @(lambda f: f.read().upper()) < o.txt
            echo q | @(lambda f: print("to-stderr", file=sys.stderr)) 2> e.txt
            cat e.txt
            @(lambda f: f.read().strip()) <<< here
            """, "a\nb\nA\nB\nto-stderr\nhere")

    def test_function_locals_are_visible(self):
        self.check("""
            def run():
                suffix = "!"
                return !(echo hi | @(lambda f: f.read().strip() + suffix)).value
            print(run())
            """, "hi!\n")

    def test_value_and_captured_output(self):
        self.check("""
            r = !(printf 'p q' | @(lambda f: f.read().split()))
            print(r.value, repr(r.out), r.returncode)
            print($(printf 'z' | @(lambda f: f.read() * 3)))
            r = !(echo | @(lambda f: (n for n in (1, 2))))
            print(r.value, r.out.split())
            """, "['p', 'q'] 'p\\nq\\n' 0\nzzz\n[1, 2] ['1', '2']\n")

    def test_builtins_and_function_aliases_take_part(self):
        self.check("""
            aliases["hi"] = lambda args: "hi " + " ".join(args)
            hi there | @(lambda f: f.read().upper())
            pwd | @(lambda f: "ok" if f.read().strip().startswith("/") else "no")
            """, "HI THERE\nok")

    def test_errors_are_reported_and_the_script_goes_on(self):
        r = self.sh("""
            def bad(f): raise ValueError("boom")
            echo | @bad
            print(last.returncode)
            echo | @nosuch
            x = 1
            echo | @x
            echo | @x.y
            echo alive
            """)
        self.assertEqual(r.stdout, "1\nalive\n", r.stderr)
        for words in ("ValueError: boom", "name 'nosuch' is not defined", "@x is not callable", "AttributeError"):
            self.assertIn(words, r.stderr)
        self.assertNotIn("run_python_pipeline", r.stderr)                    # the traceback shows the user's frames

    def test_json_loads_gets_a_hint(self):
        r = self.sh("import json\necho 1 | x=@json.loads\n")
        self.assertIn("did you mean @json.load?", r.stderr)

    def test_errexit(self):
        r = self.sh("def bad(f): raise ValueError()\necho | @bad\necho NOT\n", "-e")
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)

    def test_quoted_and_escaped_words_are_ordinary(self):
        r = self.sh("echo '@x' \\@y a@b \"@z\"\necho | '@q'\necho | \\@r\n")
        self.assertEqual(r.stdout, "@x @y a@b @z\n")
        self.assertIn("@q: command not found", r.stderr); self.assertIn("@r: command not found", r.stderr)

    def test_non_callable_expression_still_splices(self):
        self.check("""
            cmd = ["tr", "a-z", "A-Z"]
            tool = "cat"
            echo hi | @(cmd)
            echo yo | @(tool) -
            """, "HI\nyo\n")


class Streaming(Case):
    def test_middle_and_final_stages_see_lines_as_they_arrive(self):
        r = self.sh("""
            import time
            def stamp(f):
                for line in f: yield f"{line.strip()} {time.time() - t < 1.5}"
            def final(f):
                for line in f: print(line.strip(), time.time() - t < 1.5)
            t = time.time()
            for line in ![sh -c 'echo one; sleep 2; echo two' | @stamp | cat]: print(line, time.time() - t < 1.5)
            t = time.time()
            sh -c 'echo A; sleep 2; echo B' | @final
            """)
        self.assertEqual(r.stdout, "one True True\ntwo False False\nA True\nB False\n", r.stderr)

    def test_a_reader_that_stops_early_is_not_a_failure(self):
        self.check("""
            seq 200000 | first=@(lambda f: int(f.readline()))
            print(first, last.returncode)
            def forever():
                while True: yield "y"
            @forever | head -2
            seq 200000 | all=@ | head -1
            """, "1 0\ny\ny\n1\n")

    def test_large_data_passes_intact(self):
        self.check("""
            seq 300000 | raw=@ | @(lambda f: (l for l in f)) | n=@(lambda f: sum(1 for _ in f))
            print(n, len(raw.split()))
            """, "300000 300000\n")


class Capture(Case):
    def test_assignment_forms(self):
        self.check("""
            import json
            printf '{"a":1}' > c.json
            printf '{"b":2}' | data=@json.load
            conf=@json.load < c.json
            printf 'b\\na\\n' | files=@lines
            printf 'g\\n' | gen=@(lambda f: (l.strip() for l in f))
            echo today | today=@
            echo e | e=@(lambda f: None)
            print(data, conf, files, gen, repr(today), e)
            """, "{'b': 2} {'a': 1} ['b', 'a'] ['g'] 'today\\n' None\n")

    def test_raw_and_tee_anywhere(self):
        self.check("""
            printf '{"k":1}' | raw=@ | @(lambda f: len(f.read()))
            printf 'x\\n' | mid=@tee | tr x X
            printf 'y\\n' | end=@tee
            printf 'z\\n' | silent=@
            printf 'w\\n' | @tee | tr w W
            printf 'b\\na\\n' | before=@ | sort | after=@lines
            print(repr(raw), repr(mid), repr(end), repr(silent), repr(before), after)
            """, """
            7
            x
            X
            y
            w
            W
            '{"k":1}' 'x\\n' 'y\\n' 'z\\n' 'b\\na\\n' ['a', 'b']
            """)

    def test_tee_inside_command_substitution(self):
        self.check("""
            a = $(echo q | t=@tee)
            print(repr(a), repr(t))
            b = $(echo q | u=@)
            print(repr(b), repr(u))
            """, "'q' 'q\\n'\n'' 'q\\n'\n")

    def test_earlier_failure_leaves_the_variable_alone(self):
        r = self.sh("""
            x = y = z = "old"
            false | x=@lines
            print(x, last.returncode)
            sh -c 'echo partial; exit 22' | y=@
            print(y, last.returncode)
            nosuchcommand | z=@lines
            print(z, last.returncode)
            """)
        self.assertEqual(r.stdout, "old 1\nold 22\nold 127\n", r.stderr)
        self.assertIn("x not assigned: false exited with status 1", r.stderr)
        self.assertIn("y not assigned: sh exited with status 22", r.stderr)

    def test_a_later_failure_keeps_earlier_captures(self):
        r = self.sh("""
            import json
            d = "old"
            echo 'not json' | raw=@ | d=@json.load
            print(repr(raw), d, last.returncode)
            echo a | w=@ | false
            print(repr(w), last.returncode)
            """)
        self.assertEqual(r.stdout, "'not json\\n' old 1\n'a\\n' 1\n", r.stderr)

    def test_raising_function_leaves_the_variable_alone(self):
        r = self.sh("""
            def bad(f): raise ValueError()
            def half(f):
                yield 1
                raise KeyError()
            d = h = "old"
            echo | d=@bad
            echo | h=@half
            print(d, h, last.returncode)
            """)
        self.assertEqual(r.stdout, "old old 1\n", r.stderr)

    def test_environment_prefix_still_works(self):
        self.check("""
            X=@lit sh -c 'echo $X'
            Y='@q' sh -c 'echo $Y'
            echo | Z='@r' sh -c 'echo $Z'
            """, "@lit\n@q\n@r\n")

    def test_misuse_is_reported(self):
        for line, words in (("echo | a=@lines | cat", "must be the last stage"), ("echo | a=@ &", "can't be used with &"),
                            ("echo | a=@tee extra", "takes no arguments"), ("echo | @tee extra", "takes no arguments"),
                            ("echo | for=@lines", "cannot assign to for")):
            with self.subTest(line=line):
                r = self.sh(line + "\necho alive\n")
                self.assertIn(words, r.stderr); self.assertIn("alive", r.stdout)
        r = self.sh("for l in ![echo | a=@]: pass\n")
        self.assertIn("can't be used", r.stderr)


class NoMatch(Case):
    def test_empty_value_of_the_right_type_and_status_1(self):
        self.check("""
            hits = raw = n = "old"
            printf 'a\\n' | grep zebra | hits=@lines
            print(hits, last.returncode)
            printf 'a\\n' | grep zebra | raw=@
            print(repr(raw), last.returncode)
            printf 'a\\n' | grep zebra | n=@(lambda f: sum(1 for _ in f))
            print(n, last.returncode)
            printf 'a\\n' | grep zebra | early=@ | cat
            print(repr(early), last.returncode)
            """, "[] 1\n'' 1\n0 1\n'' 1\n")

    def test_and_or(self):
        self.check("""
            printf 'a\\n' | grep zebra | h=@lines && echo found
            printf 'a\\n' | grep zebra | h=@lines || echo none
            printf 'a\\n' | grep a | h=@lines && echo found
            """, "none\nfound\n")

    def test_errexit_carries_on(self):
        self.check("""
            printf 'a\\n' | grep zebra | hits=@lines
            print(len(hits))
            """, "0\n", "-e")

    def test_errexit_still_stops_for_real_failures(self):
        r = self.sh("grep x /no/such/file | h=@lines\necho NOT\n", "-e")
        self.assertEqual((r.returncode, r.stdout), (2, ""), r.stderr)
        r = self.sh("false | h=@lines\necho NOT\n", "-e")
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)
        r = self.sh("echo a | k=@ | grep zebra\necho NOT\n", "-e")            # grep after the capture: ordinary status
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)

    def test_json_cannot_be_empty(self):
        r = self.sh("import json\nd = 'old'\necho a | grep zebra | d=@json.load\nprint(d)\n")
        self.assertEqual(r.stdout, "old\n"); self.assertIn("JSONDecodeError", r.stderr)

    def test_the_list_of_commands_can_be_extended(self):
        self.check("""
            h = "old"
            no_match_commands |= {"false"}
            false | h=@lines
            print(h)
            """, "[]\n")


class Source(Case):
    def test_values_and_functions(self):
        self.check("""
            rows = ["pear", "apple"]
            @rows | sort
            @(rows) | sort -r
            @("one string") | wc -c | tr -d ' '
            def gen(n="2"):
                for i in range(int(n)): yield i
            @gen | paste -sd,
            @gen 4 | paste -sd,
            @(rows) > saved.txt
            cat saved.txt
            @({"k": 1}) | cat
            """, "apple\npear\npear\napple\n10\n0,1\n0,1,2,3\npear\napple\n{'k': 1}\n")

    def test_source_then_assignment(self):
        self.check("""
            rows = ["b", "a"]
            @rows | sort | got=@lines
            print(got)
            """, "['a', 'b']\n")

    def test_with_input_redirect_it_is_an_ordinary_stage(self):
        self.check("""
            echo data > f.txt
            @(lambda f: f.read().upper()) < f.txt | cat
            """, "DATA\n")

    def test_decorators_are_still_decorators(self):
        self.check("""
            import functools
            def tag(f): return lambda: "<" + f() + ">"
            @tag
            def a(): return "a"
            @functools.lru_cache(maxsize=2 if 1 > 0 else 3)
            def b(x): return x | 1
            class C:
                @property
                def v(self): return 5
            @(tag)
            def c(): return "c"
            print(a(), b(2), C().v, c())
            """, "<a> 3 5 <c>\n")

    def test_splice_with_words_at_the_start_of_a_line_is_a_command(self):
        self.check("""
            editor = "echo"
            @(editor) spliced words
            """, "spliced words\n")


class PrintingSource(Case):
    """A source `@(...)` runs inside its stage, so what it prints goes down the pipe."""
    SETUP = "import pprint\np = lambda x: print(pprint.pformat(x))\nrepo = {'name': 'cpython'}\n"

    def go(self, script, expected):
        self.check(self.SETUP + textwrap.dedent(script), expected)

    def test_printed_output_reaches_the_pipe(self):
        self.go("@(p(repo)) | tr a-z A-Z\n", "{'NAME': 'CPYTHON'}\n")
        self.go("@(p(repo)) > o.txt\necho saved\ncat o.txt\n", "saved\n{'name': 'cpython'}\n")
        self.go("@(print('printed') or 'returned') | cat\n", "printed\nreturned")

    def test_evaluated_exactly_once(self):
        self.go("""
            def once():
                with open("count", "a") as f: f.write("x")
                return "v"
            @(once()) | cat
            echo
            cat count
            """, "v\nx")

    def test_a_failing_source_leaves_captures_alone_and_shows_the_users_frames(self):
        r = self.sh("def f():\n    raise ValueError('inner')\nout = 'old'\n@(f()) | out=@lines\nprint(out, last.returncode)\n")
        self.assertEqual(r.stdout, "old 1\n", r.stderr)
        self.assertIn("ValueError: inner", r.stderr)
        self.assertNotIn("_evaluate", r.stderr); self.assertNotIn("_run_function", r.stderr)


class BarePythonSource(Case):
    """`p(repo) | less` without `@(...)`: a pipeline only when, as Python, it could only be a NameError."""
    SETUP = "import pprint\np = lambda x: print(pprint.pformat(x))\nrepo = {'name': 'cpython'}\n"

    def go(self, script, expected):
        self.check(self.SETUP + textwrap.dedent(script), expected)

    def test_calls_variables_and_expressions(self):
        self.go("p(repo) | tr a-z A-Z\n", "{'NAME': 'CPYTHON'}\n")
        self.go("repo | grep -o cpython\n", "cpython\n")
        self.go("[3, 1, 2] | sort\n", "1\n2\n3\n")
        self.go("sorted(repo) | paste -sd,\n", "name\n")
        self.go("print('a b') | wc -w\n", "2\n")
        self.go("(c for c in 'ba') | sort | head -1\n", "a\n")
        self.go("repo['name'] | tr c C\n", "Cpython")
        self.go("repo.keys | @(lambda f: f.read())\n", "dict_keys(['name'])\n")
        self.go("[x * 2 for x in range(3)] | tail -1\n", "4\n")

    def test_with_captures_stages_and_operators(self):
        self.go("p(repo) | out=@lines\nprint(out)\n", """["{'name': 'cpython'}"]\n""")
        self.go("p(repo) | @(lambda f: len(f.read()))\n", "20\n")
        self.go("p(repo) | grep zzz || echo none\n", "none\n")
        self.go("p(repo) | cat && echo done\n", "{'name': 'cpython'}\ndone\n")
        self.go("p(repo) |& cat; echo next\n", "{'name': 'cpython'}\nnext\n")
        self.go("print(!(repo | tr c C).out.strip())\n", "{'name': 'Cpython'}\n")
        self.go("def f():\n    mine = 'local'\n    return !(mine | cat).out\nprint(f())\n", "local\n")

    def test_other_spellings_of_the_program(self):
        with open(os.path.join(self.d, "mine"), "w") as f: f.write("#!/bin/sh\ntr a-z A-Z\n")
        os.chmod(os.path.join(self.d, "mine"), 0o755)
        for spelling in ("/usr/bin/tr a-z A-Z", "/bin/cat | ./mine", "./mine", f"../{os.path.basename(self.d)}/mine", "~/mine",
                         "$UPPER", "$(echo ./mine)", "LC_ALL=C tr a-z A-Z", "\\tr a-z A-Z", "tr a-z A-Z | /bin/cat"):
            with self.subTest(spelling=spelling):
                self.go(f"export UPPER=./mine\np(repo) | {spelling}\n", "{'NAME': 'CPYTHON'}\n")

    def test_a_path_that_does_not_exist_is_a_missing_command(self):
        r = self.sh(self.SETUP + "p(repo) | /no/such/pager\n")
        self.assertIn("/no/such/pager: command not found", r.stderr)
        self.assertNotIn("p(repo): command not found", r.stderr)

    def test_python_operators_that_look_similar_stay_python(self):
        self.go("""
            a = 6; b = 3
            print(a | ~b)
            print(a | .5.__int__())
            print(a | b == 7, a | b != 7, a | b <= 7)
            c = a | b
            c |= 8
            print(c, a | $((4)))
            """, "-2\n6\nTrue False True\n15 6\n")

    def test_python_stays_python_when_names_are_defined(self):
        self.go("""
            flags = 5; mask = 3
            flags | mask
            print(flags | mask)
            x = {1} | {2}
            print(x)
            def less(v): return v
            y = flags | less(mask)
            print(y)
            """, "7\n{1, 2}\n7\n")
        r = self.sh("a = 1\na | no_such_thing_anywhere\n")
        self.assertIn("NameError", r.stderr)

    def test_shell_pipelines_are_untouched(self):
        self.go("""
            echo hi | cat
            id | cut -c1-4
            printf 'b\\na\\n' | sort | head -1
            sh | cat < /dev/null
            echo end
            """, "hi\nuid=\na\nend\n")

    def test_percent_forces_the_shell_reading(self):
        r = self.sh("repo = 1\n% repo | cat\n")
        self.assertIn("repo: command not found", r.stderr)


class PipeFail(Case):
    def test_off_by_default(self):
        self.check("false | true\nprint(last.returncode)\nfalse | @(lambda f: None)\nprint(last.returncode)\n", "0\n0\n")

    def test_any_stage_fails_the_pipeline(self):
        self.check("""
            set_pipefail = True
            def bad(f): raise SystemExit(4)
            sh -c 'exit 3' | cat
            print(last.returncode)
            sh -c 'exit 3' | @(lambda f: None)
            print(last.returncode)
            sh -c 'exit 3' | sh -c 'exit 5' | cat
            print(last.returncode)
            sh -c 'exit 3' | false
            print(last.returncode)
            echo | @bad | cat
            print(last.returncode)
            true | true
            print(last.returncode)
            print(!(false | cat).returncode, !(false | @(lambda f: 1)).returncode)
            """, "3\n3\n5\n1\n4\n0\n1 1\n")

    def test_builtins_missing_commands_and_streams(self):
        r = self.sh("""
            set_pipefail = True
            % nosuchcommand | cat
            print(last.returncode)
            p = ![sh -c 'exit 6' | cat]
            print(p.wait())
            """)
        self.assertEqual(r.stdout, "127\n6\n", r.stderr)

    def test_a_cut_off_writer_is_not_a_failure(self):
        self.check("set_pipefail = True\nyes | head -1\nprint(last.returncode)\n", "y\n0\n")

    def test_errexit_stops_on_a_hidden_failure(self):
        r = self.sh("set_pipefail = True\nfalse | cat\necho NOT\n", "-e")
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_interactive_and_background_jobs(self):
        s = Session(rows=30, cols=80, home=self.d); self.addCleanup(s.close)
        s.send("set_pipefail = True\r", 0.5)
        s.send("sh -c 'exit 7' | cat\r", 1.0)
        self.assertIn("[7]>", s.text())
        s.send("sh -c 'sleep 2; exit 8' | cat &\r", 0.5); s.send("wait\r", 3.0)     # still running when `wait` starts
        self.assertIn("[8]>", s.text())


class Interactive(Case):
    def start(self):
        s = Session(rows=40, cols=100, home=self.d); self.addCleanup(s.close)
        return s

    def send(self, s, data, wait=0.8):
        s.send(data, wait)
        return s.text()

    def alive(self, s, word="still-here"):
        self.assertIn(f"\n{word}\n", self.send(s, f"echo {word}\r") + "\n")

    def test_returned_value_goes_to_underscore(self):
        s = self.start()
        self.send(s, "echo 41 | @(lambda f: int(f.read()))\r")
        self.assertIn("\n42", self.send(s, "_ + 1\r"))

    def test_a_consumed_iterator_is_not_put_in_underscore(self):
        s = self.start()
        self.send(s, "'untouched'\r")                  # a bare expression sets _, as in python
        self.send(s, "echo | @(lambda f: (i for i in [1]))\r")
        self.assertEqual(self.send(s, "_\r").count("\n'untouched'"), 2)     # the first echo, and `_` again

    def test_assignment_prints_nothing_and_persists(self):
        s = self.start()
        text = self.send(s, "printf 'q\\n' | got=@lines\r")
        self.assertEqual(text.rstrip().split("\n")[-2].split("> ")[-1], "printf 'q\\n' | got=@lines")
        self.assertIn("\n['q']", self.send(s, "got\r"))

    def test_ctrl_c_in_a_final_python_stage(self):
        s = self.start()
        self.send(s, "x = 'old'\r")
        s.send("sleep 20 | x=@lines\r", 0.8)
        t = time.time()
        self.send(s, CTRL_C, 1.0)
        self.alive(s)
        self.assertLess(time.time() - t, 8)
        self.assertIn("\n'old'", self.send(s, "x\r"))
        self.assertNotIn("run_python_pipeline", s.text())

    def test_ctrl_c_while_python_is_busy(self):
        s = self.start()
        s.send("import time\r", 0.4)
        s.send("echo | @(lambda f: time.sleep(20))\r", 0.8)
        self.send(s, CTRL_C, 1.0)
        self.alive(s)

    def test_ctrl_c_with_a_forked_stage_in_the_middle(self):
        s = self.start()
        s.send("sleep 20 | @(lambda f: f.read()) | cat\r", 0.8)
        self.send(s, CTRL_C, 1.0)
        self.alive(s)

    def test_ctrl_z_does_not_wedge_a_pipeline_that_ends_in_python(self):
        s = self.start()
        s.send("sleep 2 | got=@lines\r", 0.5)
        s.send(CTRL_Z, 0.5)
        time.sleep(2)
        s.pump(0.5)
        self.alive(s)

    def test_a_pipeline_ending_in_a_program_has_job_control(self):
        s = self.start()
        s.send("sleep 20 | @(lambda f: f.read()) | cat\r", 0.8)
        self.assertIn("Stopped", self.send(s, CTRL_Z, 1.0))
        self.send(s, "kill %1\r", 1.0)
        self.alive(s)

    def test_background_job(self):
        s = self.start()
        self.send(s, "printf 'bg\\n' | @(lambda f: f.read().upper()) > out.txt &\r", 1.0)
        self.send(s, "wait\r", 1.0)
        self.assertIn("\nBG", self.send(s, "cat out.txt\r"))

    def test_terminal_stays_usable_after_a_missing_command(self):
        s = self.start()
        self.assertIn("command not found", self.send(s, "nosuch-cmd arg | got=@lines\r"))
        self.alive(s)
        self.send(s, "echo a | @(lambda f: f.read()) | nosuch-cmd arg\r")
        self.alive(s, "again")

    def test_interactive_program_before_a_capture_can_still_read_the_terminal(self):
        s = self.start()
        s.send("head -1 | typed=@\r", 0.8)
        self.send(s, "from-keyboard\r", 1.0)
        self.assertIn("\n'from-keyboard\\n'", self.send(s, "typed\r"))


if __name__ == "__main__":
    unittest.main()

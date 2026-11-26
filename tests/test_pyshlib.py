"""pyshlib (the opt-in extras), the `@name(...)` stage form, last.statuses, and the history record."""
import json, os, shutil, stat, subprocess, tempfile, textwrap, time, unittest

from helpers import PYSH
from term import Session


class Case(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="pysh-test-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def sh(self, script, *opts, lib=True, timeout=60):
        path = os.path.join(self.d, "t.pysh")
        head = "from pyshlib import *\n" if lib else ""
        with open(path, "w") as f: f.write(head + textwrap.dedent(script).lstrip("\n"))
        return subprocess.run([PYSH, *opts, path], capture_output=True, text=True, timeout=timeout, cwd=self.d,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, HOME=self.d))

    def check(self, script, expected, **kw):
        r = self.sh(script, **kw)
        self.assertEqual((r.stdout, r.stderr), (textwrap.dedent(expected).lstrip("\n"), ""), msg=repr(r))


class Importing(Case):
    def test_only_what_you_import_is_defined(self):
        self.check("""
            from pyshlib import freq
            print(callable(freq), "table" in globals())
            import pyshlib
            printf 'a\\n' | @pyshlib.freq
            """, "True False\n1  ████████████████████  a\n", lib=False)

    def test_not_loaded_unless_imported(self):
        self.check("import sys\nprint('pyshlib' in sys.modules)\n", "False\n", lib=False)

    def test_a_file_in_the_current_directory_cannot_replace_it(self):
        with open(os.path.join(self.d, "pyshlib.py"), "w") as f: f.write("print('PLANTED')\n")
        self.check("print(len(__import__('pyshlib').__all__))\n", "15\n", lib=False)

    def test_imported_names_do_not_hide_shell_commands(self):
        s_out = self.sh("cd /\npwd\nenv | grep -c PATH > /dev/null && echo env-ok\n").stdout
        self.assertEqual(s_out, "/\nenv-ok\n")


class CallStages(Case):
    """`@name(...)` is a stage, spaces and all."""
    def test_in_every_position(self):
        self.check("""
            def add(n): return lambda f: [str(int(l) + n) for l in f]
            def count(a, b=1): return (str(i) for i in range(a * b))
            printf '1\\n2\\n' | @add(10)
            printf '1\\n' | @add(1) | @add(2)
            @count(2, b=2) | paste -sd,
            @add(5) < /dev/null | cat
            seq 3 | x=@(add(0))
            seq 3 | y=@add(100)
            print(x, y)
            """, "11\n12\n4\n0,1,2,3\n['1', '2', '3'] ['101', '102', '103']\n", lib=False)

    def test_lambdas_with_spaces_and_pipes_inside(self):
        self.check("""
            printf 'a.py\\nb.c\\nd.sh\\n' | @where(lambda l: l.endswith(".py") | l.endswith(".sh"))
            """, "a.py\nd.sh\n")

    def test_arguments_to_commands_stay_text(self):
        self.check("""
            echo "@f(a, b)" @g(x) a@h(y)
            X=@i(z) sh -c 'echo $X'
            git_like=1
            echo @{u} @ 2@
            """, "@f(a, b) @g(x) a@h(y)\n@i(z)\n@{u} @ 2@\n", lib=False)

    def test_must_return_a_function_when_there_is_input(self):
        r = self.sh("def five(): return 5\necho | @five()\necho alive\n", lib=False)
        self.assertIn("@five() did not return a function", r.stderr)
        self.assertEqual(r.stdout, "alive\n")

    def test_decorators_with_arguments_are_untouched(self):
        self.check("""
            import functools
            @functools.lru_cache(maxsize=1)
            def f(x): return x
            print(f(2))
            """, "2\n", lib=False)


class Stages(Case):
    def test_table(self):
        self.check("""
            printf 'NAME  PID CMD\\nsh 10 a b c\\nvim 2000 x\\n' | @table
            printf 'a,b\\n1,"x,y"\\n' | t=@table ,
            print(t[0], t["b"], len(t), t.columns)
            printf '1 2\\n3 4\\n' | n=@table noheader
            print(n.columns, n["2"])
            print(int(t.df()["a"].sum()) if __import__("importlib").util.find_spec("pandas") else 1)
            """, """
            NAME   PID  CMD
            sh      10  a b c
            vim   2000  x
            {'a': '1', 'b': 'x,y'} ['x,y'] 1 ['a', 'b']
            ['1', '2'] ['2', '4']
            1
            """)

    def test_table_header_wider_than_rows_and_empty_and_hostile(self):
        self.check("""
            printf 'Used Mounted on\\n5%% /\\n7%% /mnt\\n' | t=@table
            print(t.columns, t["Mounted on"])
            true | e=@table
            print(len(e), repr(str(e)))
            printf 'h\\n\\033]0;pwned\\007\\n' | @table
            """, "['Used', 'Mounted on'] ['/', '/mnt']\n0 ''\nh\n?]0;pwned?\n")

    def test_fields(self):
        self.check("""
            printf 'a b c\\nd e\\n' | @fields 3 1
            printf 'a b c\\n' | @fields -1
            printf 'a b c\\n' | @fields(2)
            """, "c\ta\n\td\nc\nb\n")
        self.assertIn("starting at 1", self.sh("echo a | @fields 0\n").stderr)

    def test_where_and_each(self):
        self.check("""
            printf 'ab\\ncd\\n' | @where(lambda l: "a" in l)
            printf 'ab\\ncd\\n' | @each(str.upper)
            printf '1\\n2\\n3\\n' | @each(lambda l: int(l) if l != "2" else None) | paste -sd+
            print(list(where(str.isdigit)(["1", "x"])))
            """, "ab\nAB\nCD\n1+3\n['1']\n")
        self.assertIn("write @where(function)", self.sh("echo | @where x\n").stderr)

    def test_freq(self):
        self.check("""
            printf 'b\\na\\nb\\nc\\nb\\na\\n' | f=@freq
            print(f["b"], f["zzz"], f[0], len(f))
            printf 'x\\nx\\ny\\n' | @freq 1
            printf 'x\\n' | @freq(5)
            true | @freq
            """, "3 0 ('b', 3) 3\n2  ████████████████████  x\n1  ████████████████████  x\n")

    def test_stats(self):
        self.check("""
            seq 1 5 | s=@stats
            print(s.count, s.min, s.mean, s.median, s.max, s.sum, s.skipped)
            printf 'a 10\\nb 30\\nc x\\nd nan\\n' | t=@stats 2
            print(t.count, t.mean, t.skipped)
            true | e=@stats
            print(e.count, e.mean, e)
            printf '50%%\\n' | @stats
            """, "5 1.0 3.0 3.0 5.0 15.0 0\n2 20.0 2\n0 None no numbers\ncount 1  min 50  mean 50  median 50  p95 50  max 50  sum 50  stdev -\n")

    def test_jsonl_streams_and_skips_bad_lines(self):
        r = self.sh("""
            printf '{"a":1}\\n\\noops\\nbad\\n[2]\\n' | v=@jsonl
            print(v)
            """)
        self.assertEqual(r.stdout, "[{'a': 1}, [2]]\n")
        self.assertEqual(r.stderr.count("is not JSON"), 1)
        self.check("""
            import time
            t = time.time()
            sh -c 'echo 1; sleep 2' | @jsonl | @(lambda f: [print(l.strip(), time.time() - t < 1.5) for l in f] and None)
            """, "1 True\n")

    def test_spark(self):
        self.check("""
            seq 0 7 | @spark
            printf 'a 5\\nb 5\\n' | @spark 2
            print(spark([3, 1, 2]))
            true | s=@spark
            print(repr(str(s)))
            """, "▁▂▃▄▅▆▇█\n▁▁\n█▁▅\n''\n")

    def test_peek_shows_the_start_and_passes_everything_on(self):
        r = self.sh("seq 50 | @peek 2 | wc -l\nseq 9 | @peek | tail -1\n")
        self.assertEqual(r.stdout, "50\n9\n")
        self.assertEqual(r.stderr, "1\n2\n1\n2\n3\n4\n5\n")


class Par(Case):
    def test_results_come_back_in_order_with_their_items(self):
        self.check("""
            for r in par([0.3, 0.1, 0.2], "sh -c 'sleep $0; echo $0' {}", jobs=3): print(r.item, r.out.strip(), r.returncode)
            print(par("abc", str.upper))
            """, "0.3 0.3 0\n0.1 0.1 0\n0.2 0.2 0\n['A', 'B', 'C']\n")

    def test_as_a_pipeline_stage_each_line_is_an_item_without_its_newline(self):
        self.check("""
            printf 'a b\\nc\\n' | @par 'printf [%s] {}'
            printf 'a\\nb' | r=@par 'echo {}'
            print([x.item for x in r])
            with open("items", "w") as f: f.write("x\\ny\\n")
            with open("items") as f: print([x.item for x in par(f, "true {}")])
            """, "[a b]\n[c]\n['a', 'b']\n['x', 'y']\n")

    def test_a_stage_shows_errors_and_fails_after_trying_every_item(self):
        r = self.sh("""
            both = "sh -c 'echo out$0; echo err$0 >&2; exit $0' {}"
            status = "sh -c 'exit $0' {}"
            printf '1\\n0\\n' | @par @(both) 1
            print(last.returncode)
            printf '3\\n' | @par @(status)
            printf 'a\\n' | @par 'true {}'
            print(last.returncode)
            printf '1\\n' | r=@par 'false {}'
            print(last.returncode, r[0].returncode)
            print(len(par(['a'], "false {}")))
            """)
        self.assertEqual(r.stdout, "out1\nout0\n1\n0\n0 1\n1\n", r.stderr)
        self.assertEqual(r.stderr, "err1\nerr0\npar: 3: exit status 3\n")

    def test_a_failed_stage_stops_an_errexit_script(self):
        r = self.sh("""both = "sh -c 'echo $0; exit $0' {}"\nprintf '1\\n0\\n' | @par @(both)\necho NOT\n""", "-e")
        self.assertEqual(r.stdout, "1\n0\n", r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_runs_in_parallel(self):
        t = time.time()
        self.sh("""par(range(6), "sh -c 'sleep 1' {}", jobs=6)\n""")
        self.assertLess(time.time() - t, 4)

    def test_an_item_is_one_argument_and_never_code(self):
        self.check("""
            nasty = ['a b', '; echo INJECTED', '$(echo INJECTED)', '`id`', '*', "'\\"", 'x | cat', '@(1+1)', '&& false']
            for r in par(nasty, "printf '[%s]' {}"): print(r.out)
            """, "".join(f"[{x}]\n" for x in ['a b', '; echo INJECTED', '$(echo INJECTED)', '`id`', '*', "'\"", 'x | cat', '@(1+1)', '&& false']))

    def test_quoted_or_missing_placeholder_is_refused(self):
        for template in ("echo '{}'", 'echo "a {} b"', "echo $(echo '{}')", "echo hi"):
            with self.subTest(template=template):
                r = self.sh(f"try: par(['x'], {template!r})\nexcept ValueError as e: print(e)\n")
                self.assertIn("par:", r.stdout, r.stderr)

    def test_failures_and_timeouts_are_results_even_with_errexit(self):
        self.check("""
            out = par([0, 1], "sh -c 'exit $0' {}")
            print([r.returncode for r in out])
            slow = par([5], "sleep {}", timeout=0.5)
            print(slow[0].returncode, slow[0].timed_out)
            echo after
            """, "[0, 1]\n124 True\nafter\n", )
        r = self.sh("par([1], 'false {}')\necho still\nfalse\necho NOT\n", "-e")
        self.assertEqual(r.stdout, "still\n", r.stderr)


class Temporary(Case):
    def test_in_dir_and_in_env(self):
        os.makedirs(os.path.join(self.d, "sub"))
        self.check("""
            import os
            with in_dir("sub"):
                print(os.path.basename(os.getcwd()), os.path.basename($PWD))
            print(os.path.basename(os.getcwd()) != "sub")
            export KEEP=1
            with in_env(ZZ=2, KEEP=None):
                $[sh -c 'echo [$ZZ][$KEEP]']
            sh -c 'echo [$ZZ][$KEEP]'
            try:
                with in_env(ZZ="x"): raise KeyError()
            except KeyError: print("ZZ" in os.environ)
            """, "sub sub\nTrue\n[2][]\n[][1]\nFalse\n")


class Statuses(Case):
    def test_every_stage(self):
        self.check("""
            false | true
            print(last.statuses)
            sh -c 'exit 3' | cat | sh -c 'exit 5'
            print(last.statuses, last.returncode)
            echo hi > /dev/null
            print(last.statuses)
            false | @(lambda f: None)
            print(last.statuses)
            x = 1
            print(last.statuses, !(false | false).statuses)
            """, "[1, 0]\n[3, 0, 5] 5\n[0]\n[1, 0]\n[1, 0] [1, 1]\n", lib=False)


class Interactive(Case):
    def start(self, rc="from pyshlib import *\n"):
        with open(os.path.join(self.d, ".pyshrc"), "w") as f: f.write(rc)
        s = Session(rows=40, cols=110, home=self.d); self.addCleanup(s.close)
        return s

    def send(self, s, data, wait=0.8):
        s.send(data, wait)
        return s.text()

    def record(self):
        with open(os.path.join(self.d, ".pysh_history.jsonl")) as f: return [json.loads(l) for l in f]

    def test_record_has_directory_status_and_is_private(self):
        os.makedirs(os.path.join(self.d, "proj"))
        s = self.start("")
        for cmd in ("cd proj", "false", "echo ok", " echo secret", "1/0"): self.send(s, cmd + "\r", 0.6)
        rows = self.record()
        self.assertEqual([(r["cmd"], r["status"]) for r in rows], [("cd proj", 0), ("false", 1), ("echo ok", 0), ("1/0", 1)])
        self.assertEqual(rows[0]["cwd"], self.d)                         # where it was run from
        self.assertEqual(rows[1]["cwd"], os.path.join(self.d, "proj"))
        mode = stat.S_IMODE(os.stat(os.path.join(self.d, ".pysh_history.jsonl")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_search(self):
        os.makedirs(os.path.join(self.d, "proj"))
        s = self.start()
        for cmd in ("echo Alpha", "cd proj", "sh -c 'exit 3' alpha", "echo beta", "cd .."): self.send(s, cmd + "\r", 0.6)
        self.send(s, "clear\r", 0.5)
        self.send(s, "r = hist.search('ALPHA')\r")
        self.assertIn("\n2 [0, 3]", self.send(s, "print(len(r), [e.status for e in r])\r"))
        self.assertIn("\n[\"sh -c 'exit 3' alpha\"]", self.send(s, "print([e.cmd for e in hist.search(cwd='proj', failed=True)])\r"))
        self.send(s, "clear\r", 0.5)
        text = self.send(s, "hist.search('beta')\r")
        self.assertRegex(text, r"\n\d{4}-\d\d-\d\d \d\d:\d\d  ~/proj  echo beta")
        self.assertIn("(nothing found)", self.send(s, "hist.search('zzz-none')\r"))
        self.assertIn("\n1\n", self.send(s, "print(len(hist.search('echo', limit=1, since=600)))\r") + "\n")

    def test_the_history_builtin_still_works(self):
        s = self.start()
        self.send(s, "echo one\r", 0.5)
        self.assertRegex(self.send(s, "history\r"), r"\n\s+1  echo one")

    def test_on_cd_hook_from_rc_survives_resourcing_and_errors(self):
        os.makedirs(os.path.join(self.d, "proj"))
        s = self.start("from pyshlib import on_cd\n@on_cd\ndef hook(p): print('entered', p.rsplit('/', 1)[-1])\n"
                       "@on_cd\ndef broken(p): raise ValueError('hook broke')\n")
        self.send(s, "source ~/.pyshrc\r")
        self.send(s, "clear\r", 0.5)
        text = self.send(s, "cd proj\r")
        self.assertEqual(text.count("entered proj"), 1, text)
        self.assertIn("on_cd broken: ValueError: hook broke", text)
        self.assertNotIn("[1]>", text)
        self.assertIn("\nalive", self.send(s, "echo alive\r"))

    def test_custom_completion(self):
        s = self.start("from pyshlib import completer\n"
                       "@completer('deploy')\ndef targets(word, args):\n"
                       "    return ['staging', ('production', 'careful!'), 'bad\\x1b]0;x\\x07'] if not args else None\n"
                       "@completer('boom')\ndef boom(word, args): raise ValueError()\n")
        s.send("deploy st\t", 0.8)
        self.assertTrue(s.text().rstrip().endswith("deploy staging"), s.text())
        s.send("\x15deploy \t", 0.8)
        self.assertIn("production", s.text()); self.assertIn("careful!", s.text())
        self.assertNotIn("\x1b]0;x", s.raw.decode(errors="replace"))
        s.send("\x1b", 0.5); s.send("\x15", 0.5)
        open(os.path.join(self.d, "somefile"), "w").close()
        s.send("deploy staging somef\t", 0.8)                          # None: usual completion
        self.assertTrue(s.text().rstrip().endswith("somefile"), s.text())
        s.send("\x15boom somef\t", 0.8)                                # a broken one doesn't break the editor
        self.assertTrue(s.text().rstrip().endswith("boom somefile"), s.text())

    def test_starter_rc_mentions_pyshlib(self):
        s = Session(rows=30, cols=100, home=self.d); self.addCleanup(s.close)
        with open(os.path.join(self.d, ".pyshrc")) as f: self.assertIn("# from pyshlib import table, where, freq", f.read())


if __name__ == "__main__":
    unittest.main()

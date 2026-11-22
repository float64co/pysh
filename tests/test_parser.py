import os, tempfile
from helpers import Base, run, out


class Arithmetic(Base):
    def test_basic(self):
        self.assertOut("echo $((2 + 3 * 4))", "14")

    def test_parens_and_unary(self):
        self.assertOut("echo $(( (1+2) * -3 ))", "-9")

    def test_variables(self):
        self.assertOut("export X=5\necho $((X*2)) $(($X+1))", "10 6")

    def test_unset_variable_is_zero(self):
        self.assertOut("echo $((NOPE_UNSET + 1))", "1")

    def test_c_style_division(self):
        self.assertOut('echo "$((7/2)) $((-7/2)) $((7%3)) $((-7%3))"', "3 -3 1 -1")

    def test_comparison_and_logic(self):
        self.assertOut("echo $((1<2)) $((2<1)) $((1==1 && 2>1)) $((0 || 0))", "1 0 1 0")

    def test_bitwise_and_shift(self):
        self.assertOut("echo $((1<<4)) $((6&3)) $((6|3)) $((6^3)) $((0x10))", "16 2 7 5 16")

    def test_power(self):
        self.assertOut("echo $((2**10))", "1024")

    def test_octal_literal(self):
        self.assertOut("echo $((010))", "8")

    def test_inside_double_quotes(self):
        self.assertOut('echo "n=$((1+1))"', "n=2")

    def test_division_by_zero(self):
        r = run("echo $((1/0))\necho after")
        self.assertIn("division by 0", r.err)
        self.assertIn("after", r.out)

    def test_not_a_subshell(self):
        # '$( (cmd) )' with a space is a command substitution containing a group, not arithmetic
        r = run("echo $((1+1)) ok")
        self.assertEqual(r.out.strip(), "2 ok")

    def test_python_mode(self):
        self.assertOut("print($((3*3)))", "9")

    def test_no_code_execution(self):
        r = run("echo $(( __import__('os').getpid() ))")
        self.assertNotRegex(r.out.strip(), r"^\d+$")


class Braces(Base):
    def test_list(self):
        self.assertOut("echo {a,b,c}", "a b c")

    def test_prefix_suffix(self):
        self.assertOut("echo pre{a,b}post", "preapost prebpost")

    def test_range(self):
        self.assertOut("echo {1..5}", "1 2 3 4 5")

    def test_reverse_range(self):
        self.assertOut("echo {5..1}", "5 4 3 2 1")

    def test_step(self):
        self.assertOut("echo {1..10..3}", "1 4 7 10")

    def test_zero_padded(self):
        self.assertOut("echo x{08..11}", "x08 x09 x10 x11")

    def test_letters(self):
        self.assertOut("echo {a..e}", "a b c d e")

    def test_nested(self):
        self.assertOut("echo {a,b{1,2}}", "a b1 b2")

    def test_two_groups(self):
        self.assertOut("echo {a,b}{1,2}", "a1 a2 b1 b2")

    def test_empty_alternative(self):
        self.assertOut("echo x{,y}", "x xy")

    def test_no_comma_is_literal(self):
        self.assertOut("echo {a} {} x{y}", "{a} {} x{y}")

    def test_quoted_braces_literal(self):
        self.assertOut("echo '{a,b}' \"{a,b}\"", "{a,b} {a,b}")

    def test_with_variable(self):
        self.assertOut("export V=v\necho {$V,w}", "v w")

    def test_mkdir_use_case(self):
        d = tempfile.mkdtemp()
        self.assertOut(f"cd {d}\nmkdir -p a/{{x,y}}\nls a", "x\ny")

    def test_find_braces_untouched(self):
        self.assertOut("echo {}", "{}")

    def test_helper(self):
        self.assertOut("print(shell.brace_expand('a{1..3}b'))", "['a1b', 'a2b', 'a3b']")


class Heredocs(Base):
    def test_basic(self):
        self.assertOut("cat <<EOF\nline1\nline2\nEOF", "line1\nline2")

    def test_expands_variables_and_commands(self):
        self.assertOut("export X=world\ncat <<EOF\nhello $X $(echo sub) $((1+1))\nEOF", "hello world sub 2")

    def test_quoted_delimiter_is_literal(self):
        self.assertOut("export X=w\ncat <<'EOF'\n$X $(no) \\$x\nEOF", "$X $(no) \\$x")

    def test_double_quoted_delimiter_is_literal(self):
        self.assertOut('export X=w\ncat <<"EOF"\n$X\nEOF', "$X")

    def test_backslash_dollar(self):
        self.assertOut("export X=w\ncat <<EOF\n\\$X $X\nEOF", "$X w")

    def test_strip_tabs(self):
        self.assertOut("cat <<-EOF\n\tindented\n\t\tmore\n\tEOF", "indented\nmore")

    def test_pipeline_after_heredoc(self):
        self.assertOut("cat <<EOF | tr a-z A-Z\nabc\nEOF", "ABC")

    def test_two_heredocs(self):
        self.assertOut("cat <<A; cat <<B\nfirst\nA\nsecond\nB", "first\nsecond")

    def test_heredoc_to_file(self):
        d = tempfile.mkdtemp()
        self.assertOut(f"cat <<EOF > {d}/f\nx\nEOF\ncat {d}/f", "x")

    def test_empty_body(self):
        self.assertOut("cat <<EOF\nEOF\necho after", "after")

    def test_large_body_does_not_deadlock(self):
        body = "\n".join(["x" * 100] * 2000)
        self.assertOut(f"wc -l <<EOF\n{body}\nEOF", "2000")

    def test_interactive_style_stdin(self):
        self.assertOut("cat <<EOF\nvia repl\nEOF\n", "via repl", mode="stdin")

    def test_here_string(self):
        self.assertOut("export X=w\ncat <<< \"s $X\"", "s w")

    def test_here_string_word_count(self):
        self.assertOut("wc -c <<< hi", "3")

    def test_here_string_single_quoted(self):
        self.assertOut("cat <<< 'a $X'", "a $X")


class PipeErr(Base):
    def test_pipe_ampersand_merges_stderr(self):
        self.assertIn("ls:", out("ls /nonexistent_dir |& head -c 30"))

    def test_plain_pipe_does_not(self):
        r = run("ls /nonexistent_dir | wc -c")
        self.assertEqual(r.out.strip(), "0")
        self.assertIn("ls:", r.err)

    def test_pipe_ampersand_multistage(self):
        self.assertEqual(out("sh -c 'echo o; echo e >&2' |& sort"), "e\no")


class Misc(Base):
    def test_quote_inside_substitution_inside_quotes(self):
        self.assertOut("echo \"$(echo \"a  b\")\"", "a  b")

    def test_nested_substitution(self):
        self.assertOut("echo $(echo $(echo deep))", "deep")

    def test_tokenize_helper(self):
        self.assertOut("print(shell.tokenize('a | b')[1])", "('op', '|')")

    def test_empty_quoted_arg_preserved(self):
        self.assertOut("printf '<%s>' '' x", "<><x>")

    def test_split_heredocs_helper(self):
        self.assertOut("print(shell.split_heredocs('cat <<E\\nbody\\nE')[2])", "True")
        self.assertOut("print(shell.split_heredocs('cat <<E\\nbody')[2])", "False")

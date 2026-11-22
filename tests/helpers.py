import os, subprocess, tempfile, textwrap, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYSH = os.environ.get("PYSH", os.path.join(ROOT, "pysh"))


class R:
    def __init__(self, cp):
        self.out, self.err, self.rc = cp.stdout, cp.stderr, cp.returncode

    def __repr__(self): return f"R(rc={self.rc}, out={self.out!r}, err={self.err!r})"


def run(script, *args, stdin=None, env=None, cwd=None, mode="file"):
    """Run `script` in pysh.  mode: 'file' (script file), 'c' (-c), 'stdin' (piped, REPL path)."""
    e = dict(os.environ, HOME=tempfile.gettempdir(), TERM="dumb")
    e.update(env or {})
    script = textwrap.dedent(script).lstrip("\n")
    if mode == "c":
        cmd, inp = [PYSH, "-c", script, *args], stdin
    elif mode == "stdin":
        cmd, inp = [PYSH, *args], script
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".psh", delete=False) as f: f.write(script)
        cmd, inp = [PYSH, f.name, *args], stdin
    try:
        return R(subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=60, env=e, cwd=cwd))
    finally:
        if mode == "file": os.unlink(f.name)


def out(script, **kw):
    """stdout of a script, stripped of the trailing newline."""
    return run(script, **kw).out.rstrip("\n")


class Base(unittest.TestCase):
    def assertOut(self, script, expected, **kw):
        r = run(script, **kw)
        self.assertEqual(r.out.rstrip("\n"), expected, msg=repr(r))

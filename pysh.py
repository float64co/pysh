# pysh -- embedded shell logic.  Executed by pysh.c inside __main__.
import ast, builtins, codeop, glob, os, re, shutil, signal, subprocess, sys, time, traceback

VERSION = "0.1"
ns = sys.modules["__main__"].__dict__          # python-mode namespace
aliases = {}                                   # name -> str | callable(args)->str|int|None
jobs = []
HIST = os.path.expanduser(os.environ.get("PYSH_HISTFILE", "~/.pysh_history"))
history = []

# ----------------------------------------------------------------- results
class Result:
    def __init__(self, returncode=0, out=None, err=None, duration=0.0, cmd=""):
        self.returncode, self.out, self.err = returncode, out, err
        self.duration, self.cmd = duration, cmd
    def __bool__(self): return self.returncode == 0
    def __str__(self): return self.out or ""
    def __repr__(self):
        return f"<Result rc={self.returncode} {self.duration:.3f}s {self.cmd!r}>"
    def __iter__(self): return iter((self.out or "").splitlines())

last = Result()

# --------------------------------------------------------------- tokenizer
def match_paren(s, i):
    """s[i] == '(' ; return index just past the matching ')'."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            j = i + 1
            while j < n and s[j] != c:
                j += 2 if s[j] == "\\" and c == '"' else 1
            i = j
        elif c == "(": depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0: return i + 1
        i += 1
    raise SyntaxError("unbalanced ( in command")

VARRE = re.compile(r"\$(?:\{(\w+)\}|(\w+|\?|\$))")

def tokenize(s):
    toks, i, n, cur = [], 0, len(s), None
    def add(kind, text):
        nonlocal cur
        if cur is None: cur = []
        if kind == "lit" and cur and cur[-1][0] == "lit":
            cur[-1] = ("lit", cur[-1][1] + text)
        else: cur.append((kind, text))
    def flush():
        nonlocal cur
        if cur is not None: toks.append(("w", cur)); cur = None
    while i < n:
        c = s[i]
        if c in " \t\n": flush(); i += 1
        elif c == "#" and cur is None: break
        elif c in "|&;<>":
            fd = None
            if c in "<>" and cur and len(cur) == 1 and cur[0][0] == "lit" and cur[0][1] in ("1", "2"):
                fd = int(cur[0][1]); cur = None
            flush()
            two = s[i:i+2]
            if c == ">" or c == "<":
                if two == ">&" and s[i+2:i+3].isdigit():
                    toks.append(("dup", fd if fd is not None else 1, int(s[i+2]))); i += 3; continue
                if two == ">>": toks.append(("redir", fd if fd is not None else 1, ">>")); i += 2; continue
                toks.append(("redir", fd if fd is not None else (0 if c == "<" else 1), c)); i += 1
            elif two == "&>": toks.append(("redir", "both", ">")); i += 2
            elif two in ("&&", "||"): toks.append(("op", two)); i += 2
            else: toks.append(("op", c)); i += 1
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0: raise SyntaxError("unterminated '")
            add("sq", s[i+1:j]); i = j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n and s[j+1] in '"\\$`':
                    buf.append("\x00" + s[j+1]); j += 2
                elif s[j] == "$" and s[j+1:j+2] == "(":
                    k = match_paren(s, j + 1); buf.append(s[j:k]); j = k
                else: buf.append(s[j]); j += 1
            if j >= n: raise SyntaxError('unterminated "')
            add("dq", "".join(buf)); i = j + 1
        elif c == "\\":
            if i + 1 < n: add("sq", s[i+1])
            i += 2
        elif c in "$@" and s[i+1:i+2] == "(":
            j = match_paren(s, i + 1)
            add("sub" if c == "$" else "py", s[i+2:j-1]); i = j
        elif c == "$":
            m = VARRE.match(s, i)
            if m: add("var", m.group(1) or m.group(2)); i = m.end()
            else: add("lit", c); i += 1
        else:
            add("lit", c); i += 1
    flush()
    return toks

def expand_dq(text):
    """Pieces of a double-quoted string -> list of ('lit'|'var'|'sub', text)."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\x00": out.append(("sq", text[i+1])); i += 2
        elif c == "$" and text[i+1:i+2] == "(":
            j = match_paren(text, i + 1); out.append(("sub", text[i+2:j-1])); i = j
        elif c == "$" and (m := VARRE.match(text, i)):
            out.append(("var", m.group(1) or m.group(2))); i = m.end()
        else:
            if out and out[-1][0] == "sq": out.append(("sq", c))
            else: out.append(("sq", c))
            i += 1
    return out

def getvar(name):
    if name == "?": return str(last.returncode)
    if name == "$": return str(os.getpid())
    return os.environ.get(name, "")

def expand_word(segs):
    fields = [[]]                 # each field: list of (text, quoted)
    def put(t, q): fields[-1].append((t, q))
    for idx, (kind, text) in enumerate(segs):
        if kind == "lit":
            if idx == 0 and text.startswith("~"):
                m = re.match(r"~[^/]*", text); text = os.path.expanduser(m.group()) + text[m.end():]
            put(text, False)
        elif kind == "sq": put(text, True)
        elif kind == "dq":
            put("", True)
            for k, t in expand_dq(text):
                if k == "sq": put(t, True)
                elif k == "var": put(getvar(t), True)
                else: put(capture(t).rstrip("\n"), True)
        elif kind == "var":
            ws = getvar(text).split()
            if not ws: continue
            put(ws[0], True)
            for w in ws[1:]: fields.append([(w, True)])
        elif kind == "sub":
            ws = capture(text).split()
            if not ws: continue
            put(ws[0], True)
            for w in ws[1:]: fields.append([(w, True)])
        elif kind == "py":
            v = eval(text, ns)
            vs = list(v) if isinstance(v, (list, tuple, set)) else [v]
            for j, x in enumerate(vs):
                if j or not fields[-1]: pass
                if j: fields.append([])
                put(str(x), True)
    res = []
    for f in fields:
        lit = "".join(t for t, _ in f)
        if not f or (lit == "" and not any(q for _, q in f)): continue
        if any(not q and re.search(r"[*?\[]", t) for t, q in f):
            pat = "".join(t if not q else glob.escape(t) for t, q in f)
            m = sorted(glob.glob(pat))
            if m: res.extend(m); continue
        res.append(lit)
    return res

# ------------------------------------------------------------------ parser
class Stage:
    def __init__(self): self.words, self.redirs, self.env = [], [], {}

def parse(line):
    """-> list of (pipeline:list[Stage], terminator: ';' '&&' '||' '&' '')"""
    toks = tokenize(line)
    out, pipe, st, i = [], [], Stage(), 0
    while i < len(toks):
        t = toks[i]
        if t[0] == "w": st.words.append(t[1])
        elif t[0] == "redir":
            i += 1
            if i >= len(toks) or toks[i][0] != "w": raise SyntaxError("redirect needs a target")
            st.redirs.append((t[1], t[2], toks[i][1]))
        elif t[0] == "dup": st.redirs.append((t[1], "dup", t[2]))
        else:
            op = t[1]
            if not st.words and not pipe and op != ";": raise SyntaxError(f"unexpected {op}")
            if op == "|":
                if not st.words: raise SyntaxError("empty pipeline stage")
                pipe.append(st); st = Stage()
            else:
                if st.words: pipe.append(st)
                if pipe: out.append((pipe, op))
                pipe, st = [], Stage()
        i += 1
    if st.words: pipe.append(st)
    elif pipe: raise SyntaxError("trailing |")
    if pipe: out.append((pipe, ""))
    return out

# --------------------------------------------------------------- execution
def is_command(w):
    return w in BUILTINS or w in aliases or shutil.which(w) is not None

def sh_error(msg): sys.stderr.write(f"pysh: {msg}\n"); sys.stderr.flush()

def b_cd(a):
    d = a[0] if a else os.path.expanduser("~")
    if d == "-": d = os.environ.get("OLDPWD", ".")
    old = os.getcwd()
    try: os.chdir(d)
    except OSError as e: sh_error(f"cd: {d}: {e.strerror}"); return 1
    os.environ["OLDPWD"], os.environ["PWD"] = old, os.getcwd()
    return 0
def b_exit(a): raise SystemExit(int(a[0]) if a else last.returncode)
def b_export(a):
    if not a:
        for k, v in sorted(os.environ.items()): print(f"export {k}={v!r}")
    for x in a:
        k, _, v = x.partition("="); os.environ[k] = v
    return 0
def b_unset(a):
    for k in a: os.environ.pop(k, None)
    return 0
def b_alias(a):
    if not a:
        for k, v in aliases.items(): print(f"alias {k}={v!r}" if isinstance(v, str) else f"alias {k}=<py {v.__name__}>")
    for x in a:
        k, eq, v = x.partition("=")
        if eq: aliases[k] = v
        elif k in aliases: print(f"alias {k}={aliases[k]!r}")
    return 0
def b_unalias(a):
    for k in a: aliases.pop(k, None)
    return 0
def b_jobs(a):
    for n, (p, c) in enumerate(jobs, 1):
        print(f"[{n}] {'Running' if p.poll() is None else 'Done(%d)' % p.returncode}  {c}")
    jobs[:] = [(p, c) for p, c in jobs if p.poll() is None]
    return 0
def b_source(a):
    if not a: sh_error("source: filename required"); return 2
    try: run_script(open(a[0]).read())
    except OSError as e: sh_error(f"source: {a[0]}: {e.strerror}"); return 1
    return 0
def b_history(a):
    for n, h in enumerate(history, 1): print(f"{n:5}  {h}")
    return 0
def b_type(a):
    rc = 0
    for w in a:
        if w in BUILTINS: print(f"{w} is a shell builtin")
        elif w in aliases: print(f"{w} is an alias for {aliases[w]!r}")
        elif p := shutil.which(w): print(f"{w} is {p}")
        else: sh_error(f"type: {w}: not found"); rc = 1
    return rc
USAGE = """\
Usage: pysh [-c COMMAND [ARG...]] [SCRIPT [ARG...]]
       pysh [-h | --help | -V | --version]"""

HELP = USAGE + """

pysh is a shell and a Python REPL in one: each line is run as Python if it
parses as Python (and doesn't start with an unbound command name), and as a
shell command otherwise.

Options:
  -c COMMAND     run COMMAND, then exit
  -h, --help     show this help and exit
  -V, --version  show version information and exit
  SCRIPT         run a file of mixed shell/Python lines

Python <-> shell:
  $(cmd)     stdout of cmd, as a str          x = $(date +%Y)
  !(cmd)     a Result: .returncode .out .err .duration
  @(expr)    splice a Python value into a command    ls @(files)
  $VAR       environment variable; `$VAR = "x"` sets it
  last       the Result of the previous command
  aliases    dict: aliases['ll'] = 'ls -l', or a function(args) -> str|int

Shell syntax: pipes |  && || ; &  redirects > >> < 2> 2>&1 &>  quoting,
globs, ~ and $VAR expansion, VAR=x cmd prefixes.

Builtins: """ + "{builtins}" + """

Editing keys:
  Right / End / Ctrl-F   accept the grey autosuggestion (Alt-F: one word)
  Up / Down              search history for entries starting with what you typed
  Ctrl-R                 history search menu (Enter inserts, Esc cancels)
  Tab / Shift-Tab        complete; again to cycle the menu (Enter accepts)
  Ctrl-A/E/K/U/W/L/D     start, end, kill to end, kill line, kill word, clear, delete
  Alt-B / Alt-F          move by word

Files:
  ~/.pyshrc.py           run at interactive startup, in the Python namespace
  ~/.pysh_history        command history ($PYSH_HISTFILE to change)

Environment: PYSH_HOME (Python home; default is the build-time prefix),
PYTHONHOME. See pysh(1) for details."""

def b_help(a):
    print(HELP.replace("{builtins}", " ".join(sorted(BUILTINS))))
    return 0

BUILTINS = {"cd": b_cd, "exit": b_exit, "quit": b_exit, "export": b_export, "unset": b_unset,
            "alias": b_alias, "unalias": b_unalias, "jobs": b_jobs, "source": b_source,
            ".": b_source, "history": b_history, "type": b_type, "help": b_help}

def open_redirs(st, env):
    """-> (stdin, stdout, stderr, to_close)"""
    fds = {0: None, 1: None, 2: None}; close = []
    for fd, mode, tgt in st.redirs:
        if mode == "dup":
            fds[fd] = subprocess.STDOUT if (fd == 2 and tgt == 1) else fds.get(tgt); continue
        names = expand_word(tgt)
        if len(names) != 1: raise SyntaxError("ambiguous redirect")
        f = open(names[0], {"<": "rb", ">": "wb", ">>": "ab"}[mode]); close.append(f)
        if fd == "both": fds[1] = fds[2] = f
        else: fds[fd] = f
    return fds[0], fds[1], fds[2], close

def run_pipeline(pipe, capture=False, capture_err=False, background=False, cmdline=""):
    global last
    sys.stdout.flush()
    t0 = time.time()
    argvs = []
    for st in pipe:
        words, env = list(st.words), {}
        while words and len(words[0]) and words[0][0][0] == "lit" and re.match(r"[A-Za-z_]\w*=", words[0][0][1]):
            k, _, v = words[0][0][1].partition("=")
            words[0] = [("lit", v)] + words[0][1:] if (v or len(words[0]) > 1) else [("sq", "")]
            env[k] = expand_word(words[0])[0] if expand_word(words[0]) else ""
            words.pop(0)
        argv = [a for w in words for a in expand_word(w)]
        argvs.append((argv, env))
    if len(pipe) == 1 and not argvs[0][0]:           # bare VAR=val
        os.environ.update(argvs[0][1]); return Result(0, "", "", 0, cmdline)
    if len(pipe) == 1:
        argv, env = argvs[0]
        if argv and argv[0] in BUILTINS and not pipe[0].redirs:
            rc = run_inproc(lambda: BUILTINS[argv[0]](argv[1:]), capture)
            return Result(rc[0], rc[1], "", time.time() - t0, cmdline)
        if argv and callable(aliases.get(argv[0])):
            rc = run_inproc(lambda: aliases[argv[0]](argv[1:]), capture)
            return Result(rc[0], rc[1], "", time.time() - t0, cmdline)
    procs, prev, close = [], None, []
    for n, (st, (argv, env)) in enumerate(zip(pipe, argvs)):
        last_stage = n == len(pipe) - 1
        if not argv: raise SyntaxError("empty command")
        try:
            rin, rout, rerr, c = open_redirs(st, env); close += c
        except OSError as e:
            sh_error(f"{e.filename}: {e.strerror}"); return Result(1, "", "", 0, cmdline)
        stdout = rout if rout is not None else (subprocess.PIPE if (not last_stage or capture) else None)
        stderr = rerr if rerr is not None else (subprocess.PIPE if (capture_err and last_stage) else None)
        stdin = rin if rin is not None else prev
        try:
            p = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr,
                                 env={**os.environ, **env} if env else None)
        except FileNotFoundError:
            sh_error(f"{argv[0]}: command not found"); procs.append(None); prev = None; continue
        except PermissionError:
            sh_error(f"{argv[0]}: permission denied"); procs.append(None); prev = None; continue
        if prev is not None and prev is not rin: prev.close()
        prev = p.stdout if (stdout is subprocess.PIPE and not last_stage) else None
        procs.append(p)
    for f in close: f.close()
    if background:
        p = procs[-1]
        if p: jobs.append((p, cmdline)); print(f"[{len(jobs)}] {p.pid}")
        return Result(0, None, None, 0, cmdline)
    out = err = None; rc = 127
    for n, p in enumerate(procs):
        if p is None: rc = 127; continue
        while True:
            try:
                if n == len(procs) - 1 and (capture or capture_err):
                    o, e = p.communicate()
                    out = o.decode(errors="replace") if o is not None else None
                    err = e.decode(errors="replace") if e is not None else None
                else: p.wait()
                break
            except KeyboardInterrupt: pass
        rc = p.returncode if p.returncode >= 0 else 128 - p.returncode
    return Result(rc, out, err, time.time() - t0, cmdline)

def run_inproc(fn, capture):
    """Run a builtin / python alias, optionally capturing its stdout."""
    import io
    buf = io.StringIO() if capture else None
    old = sys.stdout
    if capture: sys.stdout = buf
    try: r = fn()
    except SystemExit: raise
    except Exception as e: sh_error(f"{type(e).__name__}: {e}"); r = 1
    finally: sys.stdout = old
    if isinstance(r, str):
        if capture: buf.write(r)
        else: sys.stdout.write(r if r.endswith("\n") else r + "\n")
        r = 0
    return (r if isinstance(r, int) else 0), (buf.getvalue() if capture else None)

def expand_alias(line):
    m = re.match(r"\s*([^\s|&;<>]+)", line)
    if m and isinstance(aliases.get(m.group(1)), str):
        return line[:m.start(1)] + aliases[m.group(1)] + line[m.end(1):]
    return line

def run_shell(line, capture=False, capture_err=False):
    global last
    line = expand_alias(line)
    try: items = parse(line)
    except SyntaxError as e: sh_error(f"syntax error: {e.msg if hasattr(e,'msg') and e.msg else e}"); last = Result(2, cmd=line); return last
    res, skip_until = Result(0, "" if capture else None, cmd=line), None
    for pipe, term in items:
        if skip_until == "&&" and res.returncode != 0 or skip_until == "||" and res.returncode == 0:
            skip_until = term if term in ("&&", "||") else None
            continue
        skip_until = None
        try:
            res = run_pipeline(pipe, capture and not term == "&", capture_err, term == "&", line)
        except SyntaxError as e: sh_error(f"syntax error: {e}"); res = Result(2, cmd=line)
        except OSError as e: sh_error(str(e)); res = Result(1, cmd=line)
        last = res
        if term in ("&&", "||"): skip_until = term
    return res

def capture(cmd):
    r = run_shell(cmd, capture=True)
    return r.out or ""

# ------------------------------------------------------- python integration
def preprocess(s):
    """Rewrite $(cmd) !(cmd) $NAME outside string literals into python."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            q = s[i:i+3] if s[i:i+3] in ('"""', "'''") else c
            j = s.find(q, i + len(q))
            j = n if j < 0 else j + len(q)
            out.append(s[i:j]); i = j
        elif c == "$" and s[i+1:i+2] == "(":
            j = match_paren(s, i + 1); out.append(f"__sh_out__({s[i+2:j-1]!r})"); i = j
        elif c == "!" and s[i+1:i+2] == "(":
            j = match_paren(s, i + 1); out.append(f"__sh__({s[i+2:j-1]!r})"); i = j
        elif c == "$" and (m := re.match(r"\$([A-Za-z_]\w*)", s[i:])):
            out.append(f"__env__[{m.group(1)!r}]"); i += m.end()
        else: out.append(c); i += 1
    return "".join(out)

class _Env:
    def __getitem__(s, k): return os.environ.get(k, "")
    def __setitem__(s, k, v): os.environ[k] = str(v)
    def __contains__(s, k): return k in os.environ

ns.update(__sh__=lambda c: run_shell(c, capture=True, capture_err=True),
          __sh_out__=lambda c: capture(c).rstrip("\n"), __env__=_Env(),
          aliases=aliases, Result=Result, sh=lambda c: run_shell(c, capture=True, capture_err=True))
ns["__doc__"] = None
ns["complete_ignore_dirs"] = ("/mnt/c",)   # PATH directories hidden from command completion
ns["complete_ignore"] = (".dll", ".exe", ".vbs", ".efi", ".mof", ".bat", ".drv", ".sys", ".tlb", ".xml")   # command-name suffixes hidden from Tab completion (set in ~/.pyshrc.py)

KW = {"def", "class", "if", "elif", "else", "for", "while", "with", "try", "except", "finally", "import",
      "from", "async", "return", "raise", "del", "assert", "global", "nonlocal", "pass", "lambda", "yield", "match", "await"}

def classify(line):
    s = line.strip()
    m = re.match(r"[^\s|&;<>()=]+", s)
    w = m.group() if m else ""
    if w in KW or s.startswith("@"): return "py"
    if w in BUILTINS and w not in ns and not s[len(w):].lstrip().startswith(("(", "=", ".", "[")): return "sh"
    py = preprocess(s)
    try: code = codeop.compile_command(py, "<pysh>", "single")
    except (SyntaxError, ValueError, OverflowError): return "sh"
    if code is None: return "sh"
    try: tree = ast.parse(py)
    except SyntaxError: return "sh"
    if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr) and w:
        unbound = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id not in ns and not hasattr(builtins, n.id)]
        looks_call = s[len(w):].lstrip().startswith("(")
        if (w in unbound or (unbound and w in ns is False)) and is_command(w) and not looks_call: return "sh"
        if unbound and not looks_call and is_command(w) and w not in ns: return "sh"
        if unbound and hasattr(builtins, w) and w not in ns and shutil.which(w) and not looks_call: return "sh"
    return "py"

def run_python(src):
    global last
    try:
        code = compile(preprocess(src), "<pysh>", "single")
        exec(code, ns)
    except SystemExit: raise
    except KeyboardInterrupt: print("KeyboardInterrupt")
    except NameError as e:
        if re.fullmatch(r"[\w.+-]+", src.strip()) and e.name == src.strip():
            sh_error(f"{e.name}: command not found"); last = Result(127, cmd=src); return
        traceback.print_exception(type(e), e, e.__traceback__.tb_next)
        last = Result(1, cmd=src); return
    except BaseException as e:
        tb = e.__traceback__.tb_next if e.__traceback__ and not isinstance(e, SyntaxError) else None
        traceback.print_exception(type(e), e, tb)
        last = Result(1, cmd=src)
        return
    last = Result(0, cmd=src)

def run_line(line):
    """Run one logical (possibly multi-physical-line) entry."""
    if classify(line) == "sh": return run_shell(line)
    return run_python(line)

def run_script(text):
    buf = []
    for ln in text.splitlines():
        if not buf and (not ln.strip() or ln.lstrip().startswith("#")): continue
        buf.append(ln)
        src = "\n".join(buf)
        if src.endswith("\\"): buf[-1] = ln[:-1]; continue
        if classify(buf[0]) == "py":
            try: done = codeop.compile_command(preprocess(src), "<pysh>", "exec") is not None
            except SyntaxError: done = True
            if not done or (len(buf) > 1 and ln.strip()) or (len(buf) > 1 and ln[:1] in " \t"):
                continue
        run_py_or_sh("\n".join(buf)); buf = []
    if buf: run_py_or_sh("\n".join(buf))

def run_py_or_sh(src):
    if classify(src.split("\n", 1)[0]) == "sh" and "\n" not in src: return run_shell(src)
    src = src if src.endswith("\n") else src + "\n"
    try:
        exec(compile(preprocess(src), "<pysh>", "exec"), ns)
    except SystemExit: raise
    except BaseException as e:
        traceback.print_exception(type(e), e, e.__traceback__.tb_next if not isinstance(e, SyntaxError) else None)

# --------------------------------------------------------------- interface
def git_branch():
    """Current branch (or short hash if detached) of the repo containing cwd, else None.
    Reads .git/HEAD directly instead of spawning git."""
    d = os.getcwd()
    while True:
        g = os.path.join(d, ".git")
        if os.path.exists(g): break
        if d == "/" or d == os.path.dirname(d): return None
        d = os.path.dirname(d)
    try:
        if os.path.isfile(g):                       # worktree / submodule: "gitdir: <path>"
            t = open(g).read().strip()
            if not t.startswith("gitdir:"): return None
            g = os.path.join(d, t[7:].strip())
        head = open(os.path.join(g, "HEAD")).read().strip()
    except OSError: return None
    return head[16:] if head.startswith("ref: refs/heads/") else head[:7] if head else None

def prompt():
    _cmdcache.clear()
    cwd = os.getcwd(); home = os.path.expanduser("~")
    if cwd == home or cwd.startswith(home + "/"): cwd = "~" + cwd[len(home):]
    parts = cwd.split("/")                      # fish-style prompt_pwd: shorten parents
    short = "/".join([p[:1] if p and p != "~" else p for p in parts[:-1]] + parts[-1:])
    rc = f"\x1b[31m[{last.returncode}]\x1b[0m" if last.returncode else ""
    b = git_branch()
    br = f"\x1b[37m({b})\x1b[0m" if b else ""
    user = os.environ.get("USER") or "user"
    host = os.uname().nodename.split(".")[0]
    return f"\x1b[32m{user}@{host}\x1b[0m \x1b[36m{short}\x1b[0m{br}{rc}\x1b[90m>\x1b[0m "

# ------------------------------------------------- fish-style autosuggest/highlight
_cmdcache = {}
def _valid_cmd(w):
    if w not in _cmdcache:
        _cmdcache[w] = is_command(os.path.expanduser(w)) if "/" not in w else os.access(os.path.expanduser(w), os.X_OK)
    return _cmdcache[w]

def suggest(buf):
    if not buf.strip() or "\n" in buf: return ""
    for h in reversed(history):
        if h.startswith(buf) and len(h) > len(buf) and "\n" not in h: return h[len(buf):]
    return ""

_SHTOK = re.compile(r"""(?P<ws>\s+)|(?P<str>'[^']*'?|"(?:\\.|[^"\\])*"?)|(?P<op>\|\||&&|>>|[0-9]?>&[0-9]|[|;&<>])
                        |(?P<cm>\#.*)|(?P<w>(?:\\.|[^\s|&;<>'"\\])+)|(?P<x>.)""", re.X)
_PYTOK = re.compile(r"""(?P<str>(?:[rbfRBF]{0,2})(?:\"\"\"[\s\S]*?(?:\"\"\"|$)|'[^'\n]*'?|"(?:\\.|[^"\\\n])*"?))|(?P<cm>\#.*)|(?P<n>\b\d[\w.]*)|(?P<w>[A-Za-z_]\w*)|(?P<x>[\s\S])""", re.X)
C = lambda n, t: f"\x1b[{n}m{t}\x1b[0m"

def highlight(buf):
    import keyword
    try: kind = classify(buf) if buf.strip() else "sh"
    except Exception: kind = "sh"
    out = []
    if kind == "py":
        for m in _PYTOK.finditer(buf):
            k, t = m.lastgroup, m.group()
            if k == "str": out.append(C(33, t))
            elif k == "cm": out.append(C(90, t))
            elif k == "n": out.append(C(35, t))
            elif k == "w" and keyword.iskeyword(t): out.append(C(34, t))
            elif k == "w" and t in ns and callable(ns[t]) or k == "w" and hasattr(builtins, t) and callable(getattr(builtins, t)): out.append(C(36, t))
            else: out.append(t)
        return "".join(out)
    cmd = True
    for m in _SHTOK.finditer(buf):
        k, t = m.lastgroup, m.group()
        if k == "w":
            if cmd and re.match(r"[A-Za-z_]\w*=", t): out.append(t); continue
            if cmd:
                out.append(C(34, t) if _valid_cmd(t) else C(31, t)); cmd = False
            elif t.startswith("$") or t.startswith("@("): out.append(C(36, t))
            elif os.path.exists(os.path.expanduser(t)) and not t.startswith("-"): out.append(f"\x1b[4m{t}\x1b[0m")
            else: out.append(t)
        elif k == "str": out.append(C(33, t)); cmd = False if not cmd else cmd
        elif k == "op":
            out.append(C(36, t))
            if t in ("|", "||", "&&", ";", "&"): cmd = True
        elif k == "cm": out.append(C(90, t))
        else: out.append(t)
    return "".join(out)

_helpcache = {}
def flags_for(cmd):
    """Parse `cmd --help` into [(flag, description)] (cached, 1.5s timeout)."""
    if cmd in _helpcache: return _helpcache[cmd]
    res, path = [], shutil.which(cmd)
    if path:
        try:
            r = subprocess.run([path, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=1.5, text=True, errors="replace")
            seen = set()
            for line in (r.stdout or r.stderr).splitlines():
                t = line.strip()
                if not t.startswith("-"): continue
                parts = re.split(r"\s{2,}", t, 1)
                spec, desc = parts[0], (parts[1] if len(parts) > 1 else "")
                for f in re.findall(r"(?:^|[\s,|])(--?[A-Za-z0-9][\w-]*)", spec):
                    if f not in seen: seen.add(f); res.append((f, desc.strip()[:70]))
        except Exception: pass
    _helpcache[cmd] = res
    return res

_mandb = None
def man_desc():
    """{command: one-line description} from the man-db index (`whatis`), loaded once."""
    global _mandb
    if _mandb is None:
        _mandb = {}
        try:
            r = subprocess.run(["whatis", "-l", "-s", "1,6,8", "-r", ".*"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=3, text=True, errors="replace",
                               env={**os.environ, "LC_ALL": "C"})
            for line in r.stdout.splitlines():
                m = re.match(r"(\S+)\s+\((\w+)\)\s+- (.*)", line)
                if m and (m.group(1) not in _mandb or m.group(2) == "1"): _mandb[m.group(1)] = m.group(3)
        except Exception: pass
    return _mandb

BUILTIN_DOC = {"cd": "change directory", "exit": "leave the shell", "quit": "leave the shell",
               "export": "set environment variables", "unset": "remove environment variables",
               "alias": "define or list aliases", "unalias": "remove aliases", "jobs": "list background jobs",
               "source": "run a script in this shell", ".": "run a script in this shell",
               "history": "show command history", "type": "describe how a name resolves", "help": "pysh help"}

def _pydoc(o):
    d = (getattr(o, "__doc__", None) or "").strip().split("\n")[0] if not isinstance(o, (int, str, float, list, dict, tuple, set)) else ""
    return d[:70]

def _human(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T": return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024

def _esc(w): return re.sub(r"([ \t'\"\\$&;|<>()*?#])", r"\\\1", w)

def complete(before):
    """-> (text_before_cursor_with_common_prefix, [(full_text, display, description)])"""
    m = re.search(r"[^\s|&;<>]*$", before)
    word, head = m.group(), before[:m.start()]
    stage = re.split(r"\|\||&&|[|;&]", head)[-1].split()
    first = not stage
    items, nospace = [], False          # items: (insert_word, display, desc)
    if word.startswith("$") and not word.startswith("$("):
        items = [("$" + k, "$" + k, v[:40]) for k, v in sorted(os.environ.items()) if k.startswith(word[1:])]
    elif first and "/" not in word and "." not in word and not word.startswith("~") and word:
        seen = {}
        for n, d in [(k, "builtin: " + BUILTIN_DOC.get(k, "")) for k in BUILTINS] + [(k, "alias: " + (v if isinstance(v, str) else "python")) for k, v in aliases.items()]:
            seen.setdefault(n, d)
        for k, v in ns.items():
            if not k.startswith("_") and k not in ("sh",): seen.setdefault(k, _pydoc(v) or type(v).__name__)
        for k in dir(builtins):
            if k[0].islower(): seen.setdefault(k, _pydoc(getattr(builtins, k)) or "python builtin")
        skip = tuple(x.rstrip("/") for x in ns["complete_ignore_dirs"])
        for d in os.environ.get("PATH", "").split(":"):
            if any(d == x or d.startswith(x + "/") for x in skip): continue
            try:
                for f in os.listdir(d):
                    if not f.lower().endswith(ns["complete_ignore"]): seen.setdefault(f, None)
            except OSError: pass
        man = man_desc() if any(v is None and k.startswith(word) for k, v in seen.items()) else {}
        items = [(n, n, d if d is not None else man.get(n) or "command")
                 for n, d in sorted(seen.items()) if n.startswith(word)]
    elif "." in word and "/" not in word and classify(before) == "py":
        base, _, attr = word.rpartition(".")
        try:
            obj = eval(base, ns); nospace = True
            for a in dir(obj):
                if a.startswith(attr) and (attr or not a.startswith("_")):
                    try: ty = type(getattr(obj, a)).__name__
                    except Exception: ty = ""
                    items.append((base + "." + a, a, ty))
        except Exception: pass
    elif word.startswith("-") and stage:
        items = [(f, f, d) for f, d in flags_for(stage[0]) if f.startswith(word)]
    else:
        pre = word.rpartition("/")[2]
        d = word[:len(word) - len(pre)]
        dd = os.path.expanduser(d) if d else "."
        try:
            for f in sorted(os.listdir(dd)):
                if f.startswith(pre) and (pre or not f.startswith(".")):
                    full = os.path.join(dd, f)
                    if os.path.isdir(full): items.append((_esc(d + f) + "/", f + "/", "directory"))
                    else:
                        try: sz = _human(os.stat(full).st_size)
                        except OSError: sz = ""
                        items.append((_esc(d + f), f, ("executable, " if os.access(full, os.X_OK) else "") + sz))
        except OSError: pass
    if not items: return before, []
    out = [(head + w + ("" if nospace or w.endswith("/") else " "), disp, desc) for w, disp, desc in items]
    common = os.path.commonprefix([w for w, _, _ in items])
    return head + common, out

def search_history(query):
    """Ctrl-R: newest-first, de-duplicated, case-insensitive substring matches."""
    q, seen, out = query.lower(), set(), []
    for h in reversed(history):
        if h in seen or q not in h.lower(): continue
        seen.add(h)
        out.append((h, h.replace("\n", " ⏎ "), ""))
        if len(out) >= 200: break
    return out

def load_history():
    try:
        with open(HIST) as f: history.extend(l.rstrip("\n") for l in f if l.strip())
    except OSError: pass

def save_history(line):
    if line.strip() and (not history or history[-1] != line):
        history.append(line)
        try:
            with open(HIST, "a") as f: f.write(line + "\n")
        except OSError: pass

def read_entry(tty):
    """Read one logical entry (with continuation handling)."""
    import _pyshline
    def rl(p):
        if tty: return _pyshline.readline(p, history, complete, suggest, highlight, search_history)
        sys.stdout.flush()
        l = sys.stdin.readline()
        if not l: raise EOFError
        return l.rstrip("\n")
    p = ns.get("prompt", prompt)
    first = rl(p() if callable(p) else p)
    if not first.strip(): return first
    lines = [first]
    while True:
        src = "\n".join(lines)
        if src.endswith("\\"): lines[-1] = lines[-1][:-1]; lines.append(rl("\x1b[90m…\x1b[0m ")); continue
        if classify(lines[0]) != "py": break
        try: code = codeop.compile_command(preprocess(src), "<pysh>", "single")
        except SyntaxError: break
        if code is not None and not (len(lines) > 1 and lines[-1].strip()): break
        if code is not None and len(lines) == 1 and not first.rstrip().endswith(":"): break
        lines.append(rl("\x1b[90m…\x1b[0m "))
    return "\n".join(lines)

def repl():
    tty = os.isatty(0) and os.isatty(1)
    if tty:
        load_history()
        rcf = os.path.expanduser("~/.pyshrc.py")
        if os.path.exists(rcf):
            try: exec(compile(open(rcf).read(), rcf, "exec"), ns)
            except Exception: traceback.print_exc()
    while True:
        try: line = read_entry(tty)
        except EOFError: break
        except KeyboardInterrupt: continue
        if not line.strip(): continue
        if tty: save_history(line)
        try: run_line(line)
        except SystemExit as e: return int(e.code or 0) if not isinstance(e.code, str) else 1
        except KeyboardInterrupt: print()
    if tty: print()
    return last.returncode

def main(argv):
    signal.signal(signal.SIGTSTP, signal.SIG_IGN) if os.isatty(0) else None
    if len(argv) >= 3 and argv[1] == "-c":
        sys.argv = ["pysh"] + argv[3:]
        try: run_script(argv[2])
        except SystemExit as e: return int(e.code or 0)
        return last.returncode
    if len(argv) >= 2 and argv[1] in ("-V", "--version"):
        print(f"pysh {VERSION}\nPython {sys.version}\nhome {sys.prefix}"); return 0
    if len(argv) >= 2 and argv[1] in ("-h", "--help"): print(HELP.replace("{builtins}", " ".join(sorted(BUILTINS)))); return 0
    if len(argv) >= 2 and argv[1].startswith("-") and argv[1] != "-":
        sh_error(f"unknown option {argv[1]}"); print(USAGE, file=sys.stderr); return 2
    if len(argv) >= 2:
        sys.argv = argv[1:]
        try: run_script(open(argv[1]).read())
        except SystemExit as e: return int(e.code or 0)
        except OSError as e: sh_error(f"{argv[1]}: {e.strerror}"); return 127
        return last.returncode
    return repl()

if __name__ == "__main__":
    sys.stdout.flush()
    raise SystemExit(main(sys.argv))

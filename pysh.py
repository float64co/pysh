# pysh -- embedded shell logic.  Executed by pysh.c inside __main__.
import ast, builtins, codeop, glob, inspect, io, keyword, operator, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, traceback, types

VERSION = "0.1"
shell = sys.modules["__main__"]                # pysh's own internals: shell.jobs, shell.history, ...
ns = {"__name__": "__main__", "__builtins__": builtins}   # the namespace your Python lines run in
aliases = {}                                   # name -> str | callable(args)->str|int|None
HIST = os.path.expanduser(os.environ.get("PYSH_HISTFILE", "~/.pysh_history"))
history = []

# ----------------------------------------------------------------- results
class Result:
    def __init__(self, returncode=0, out=None, err=None, duration=0.0, cmd=""):
        self.returncode, self.out, self.err = returncode, out, err
        self.duration, self.cmd = duration, cmd
        self.timed_out = False
    def __bool__(self): return self.returncode == 0
    def __str__(self): return self.out or ""
    def __repr__(self):
        return f"<Result rc={self.returncode} {self.duration:.3f}s {self.cmd!r}>"
    def __iter__(self): return iter((self.out or "").splitlines())

last = Result()

def _setlast(r):
    global last
    last = r; ns["last"] = r
    return r

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

def match_bracket(s, i):
    """s[i] == '[' ; return the index just past the matching ']'."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            j = i + 1
            while j < n and s[j] != c: j += 2 if s[j] == "\\" and c == '"' else 1
            i = j
        elif c == "[": depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0: return i + 1
        i += 1
    raise SyntaxError("unbalanced [ in command")

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
                if s[i:i+3] == "<<<": toks.append(("redir", fd if fd is not None else 0, "<<<")); i += 3; continue
                if two == "<<":
                    dash = s[i+2:i+3] == "-"
                    toks.append(("redir", fd if fd is not None else 0, "<<-" if dash else "<<")); i += 3 if dash else 2; continue
                if two == ">&" and s[i+2:i+3].isdigit():
                    toks.append(("dup", fd if fd is not None else 1, int(s[i+2]))); i += 3; continue
                if two == ">>": toks.append(("redir", fd if fd is not None else 1, ">>")); i += 2; continue
                toks.append(("redir", fd if fd is not None else (0 if c == "<" else 1), c)); i += 1
            elif two == "&>": toks.append(("redir", "both", ">")); i += 2
            elif two in ("&&", "||", "|&"): toks.append(("op", two)); i += 2
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
            inner = s[i+2:j-1]
            if c == "$" and is_arith(inner): add("arith", inner[1:-1])
            else: add("sub" if c == "$" else "py", inner)
            i = j
        elif c == "$":
            m = VARRE.match(s, i)
            if m: add("var", m.group(1) or m.group(2)); i = m.end()
            else: add("lit", c); i += 1
        else:
            add("lit", c); i += 1
    flush()
    return toks

def is_arith(inner):
    """inner is the text between '$(' and ')': '$((1+2))' gives '(1+2)'."""
    return inner.startswith("(") and inner.endswith(")") and match_paren(inner, 0) == len(inner)

_M = 1 << 64
def _wrap(x):
    """Signed 64-bit wraparound, as in bash: results stay small whatever the input."""
    x &= _M - 1
    return x - _M if x >= 1 << 63 else x

_ARITH = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge}

def arith(expr):
    """Shell-style integer arithmetic: $((1 + 2 * $X)).  Names refer to environment variables.  64-bit signed."""
    expr = re.sub(r"\$\{?(\w+)\}?", lambda m: getvar(m.group(1)) or "0", expr)
    expr = re.sub(r"\b0[0-7]+\b", lambda m: str(int(m.group(), 8)), expr)       # octal literals
    expr = expr.replace("&&", " and ").replace("||", " or ")
    try: tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError: raise SyntaxError(f"bad arithmetic expression: {expr.strip()}")
    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) is int: return _wrap(n.value)
        if isinstance(n, ast.Name):
            try: return _wrap(int(os.environ.get(n.id, "0") or "0", 0))
            except ValueError: return 0
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.USub): return _wrap(-v)
            if isinstance(n.op, ast.UAdd): return v
            if isinstance(n.op, ast.Invert): return ~v
            if isinstance(n.op, ast.Not): return int(not v)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if type(n.op) in _ARITH: return _wrap(_ARITH[type(n.op)](a, b))
            if isinstance(n.op, ast.LShift): return _wrap(a << (b & 63))
            if isinstance(n.op, ast.RShift): return a >> (b & 63)
            if isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)):
                if b == 0: raise ArithmeticError("division by 0")
                q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)       # C-style truncation
                return _wrap(q) if isinstance(n.op, (ast.Div, ast.FloorDiv)) else _wrap(a - b * q)
            if isinstance(n.op, ast.Pow): return _wrap(pow(a, b, _M)) if b >= 0 else 0
        if isinstance(n, ast.Compare):
            v = ev(n.left)
            for op, c in zip(n.ops, n.comparators):
                w = ev(c)
                if type(op) not in _CMP or not _CMP[type(op)](v, w): return 0
                v = w
            return 1
        if isinstance(n, ast.BoolOp):
            vs = [ev(x) for x in n.values]
            return int(all(vs)) if isinstance(n.op, ast.And) else int(any(vs))
        if isinstance(n, ast.IfExp): return ev(n.body) if ev(n.test) else ev(n.orelse)
        raise SyntaxError("unsupported operator in arithmetic expression")
    return ev(tree)

_BRACE_MAX = 100_000        # items in one {a..b} range
_BRACE_TOTAL = 1_000_000    # words one word can expand to

def brace_expand(s):
    """bash-style {a,b,c} and {1..5} / {a..e} / {01..10..2} expansion on a plain string."""
    i = 0
    while i < len(s):
        if s[i] != "{": i += 1; continue
        depth, j, commas = 0, i, []
        while j < len(s):
            if s[j] == "{": depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0: break
            elif s[j] == "," and depth == 1: commas.append(j)
            j += 1
        if j >= len(s): i += 1; continue
        body, alts = s[i+1:j], None
        if commas:
            cuts = [i] + commas + [j]
            alts = [s[cuts[k]+1:cuts[k+1]] for k in range(len(cuts) - 1)]
        else:
            m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", body)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                step = abs(int(m.group(3))) if m.group(3) else 1
                step = step or 1
                width = max(len(m.group(1)), len(m.group(2))) if any(len(x) > 1 and x.lstrip("-").startswith("0") for x in m.group(1, 2)) else 0
                if abs(b - a) // step + 1 > _BRACE_MAX: raise ArithmeticError("brace expansion too large")
                seq = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
                alts = [str(x).zfill(width) if width else str(x) for x in seq]
            else:
                m = re.fullmatch(r"([A-Za-z])\.\.([A-Za-z])(?:\.\.(-?\d+))?", body)
                if m:
                    a, b = ord(m.group(1)), ord(m.group(2))
                    step = abs(int(m.group(3))) if m.group(3) else 1
                    seq = range(a, b + 1, step or 1) if a <= b else range(a, b - 1, -(step or 1))
                    alts = [chr(x) for x in seq]
        if alts is None: i += 1; continue
        pre, post = s[:i], s[j+1:]
        out = []
        for a in alts:
            out += brace_expand(pre + a + post)
            if len(out) > _BRACE_TOTAL: raise ArithmeticError("brace expansion too large")
        return out
    return [s]

_PH = 0xE000     # private-use placeholders stand in for quoted/substituted segments during brace expansion

def brace_words(segs):
    if not any(k == "lit" and "{" in t for k, t in segs): return [segs]
    flat, parts = [], []
    for k, t in segs:
        if k == "lit": flat.append(t)
        else: flat.append(chr(_PH + len(parts))); parts.append((k, t))
    res = []
    for w in brace_expand("".join(flat)):
        out, buf = [], ""
        for ch in w:
            if _PH <= ord(ch) < _PH + len(parts):
                if buf: out.append(("lit", buf)); buf = ""
                out.append(parts[ord(ch) - _PH])
            else: buf += ch
        if buf: out.append(("lit", buf))
        res.append(out or [("sq", "")])
    return res

def expand_text(text):
    """Expand $VAR, $(cmd), $((expr)) in heredoc text (unquoted delimiter)."""
    text = re.sub(r"\\([\\$`])", lambda m: "\x00" + m.group(1), text)
    out = []
    for k, t in expand_dq(text):
        if k == "sq": out.append(t)
        elif k == "var": out.append(getvar(t))
        elif k == "arith": out.append(str(arith(t)))
        else: out.append(capture(t).rstrip("\n"))
    return "".join(out)

def expand_dq(text):
    """Pieces of a double-quoted string -> list of ('lit'|'var'|'sub', text)."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\x00": out.append(("sq", text[i+1])); i += 2
        elif c == "$" and text[i+1:i+2] == "(":
            j = match_paren(text, i + 1); inner = text[i+2:j-1]
            out.append(("arith", inner[1:-1]) if is_arith(inner) else ("sub", inner)); i = j
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
    return [x for w in brace_words(segs) for x in _expand_word(w)]

def _expand_word(segs):
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
                elif k == "arith": put(str(arith(t)), True)
                else: put(capture(t).rstrip("\n"), True)
        elif kind == "var":
            ws = getvar(text).split()
            if not ws: continue
            put(ws[0], True)
            for w in ws[1:]: fields.append([(w, True)])
        elif kind == "arith": put(str(arith(text)), True)
        elif kind == "sub":
            ws = capture(text).split()
            if not ws: continue
            put(ws[0], True)
            for w in ws[1:]: fields.append([(w, True)])
        elif kind == "py":
            v = eval(text, *_scope())
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
    def __init__(self): self.words, self.redirs, self.env, self.merge_err = [], [], {}, False

def heredoc_markers(line):
    """[(delimiter, quoted, strip_tabs)] for every << in a command line, in order."""
    try: toks = tokenize(line)
    except SyntaxError: return []
    out = []
    for i, t in enumerate(toks):
        if t[0] == "redir" and t[2] in ("<<", "<<-") and i + 1 < len(toks) and toks[i+1][0] == "w":
            segs = toks[i+1][1]
            out.append(("".join(x for k, x in segs if k in ("lit", "sq", "dq")),
                        any(k in ("sq", "dq") for k, _ in segs), t[2] == "<<-"))
    return out

def split_heredocs(text):
    """-> (command_line, [(body, quoted)], complete).  `text` is the line plus following body lines."""
    first, _, rest = text.partition("\n")
    marks = heredoc_markers(first)
    if not marks: return text, [], True
    lines, idx, bodies, complete = (rest.split("\n") if rest else []), 0, [], True
    for delim, quoted, strip in marks:
        body = []
        while True:
            if idx >= len(lines): complete = False; break
            ln = lines[idx]; idx += 1
            if strip: ln = ln.lstrip("\t")
            if ln == delim: break
            body.append(ln)
        bodies.append(("".join(b + "\n" for b in body), quoted))
    return first, bodies, complete

def parse(line, heredocs=None):
    """-> list of (pipeline:list[Stage], terminator: ';' '&&' '||' '&' '')"""
    heredocs = iter(heredocs or [])
    toks = tokenize(line)
    out, pipe, st, i = [], [], Stage(), 0
    while i < len(toks):
        t = toks[i]
        if t[0] == "w": st.words.append(t[1])
        elif t[0] == "redir":
            i += 1
            if i >= len(toks) or toks[i][0] != "w": raise SyntaxError("redirect needs a target")
            if t[2] in ("<<", "<<-"):
                try: st.redirs.append((t[1], "heredoc", next(heredocs)))
                except StopIteration: raise SyntaxError("here-document has no body")
            elif t[2] == "<<<": st.redirs.append((t[1], "herestr", toks[i][1]))
            else: st.redirs.append((t[1], t[2], toks[i][1]))
        elif t[0] == "dup": st.redirs.append((t[1], "dup", t[2]))
        else:
            op = t[1]
            if not st.words and not pipe and op != ";": raise SyntaxError(f"unexpected {op}")
            if op in ("|", "|&"):
                if not st.words: raise SyntaxError("empty pipeline stage")
                st.merge_err = op == "|&"
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
_cmdcache = {}          # name -> bool; cleared before every prompt so new installs are noticed
def is_command(w):
    if w in BUILTINS or w in aliases: return True
    r = _cmdcache.get(w)
    if r is None: r = _cmdcache[w] = shutil.which(w) is not None
    return r

# ------------------------------------------------------------------ trust helpers
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

def clean(s):
    """Text that came from outside (paths, branch names, history, error messages) with control characters
    shown as '?', so it can't smuggle terminal escape sequences (title changes, clipboard writes...)."""
    return _CTRL.sub("?", s)

def _root_safe(path):
    """Owned by root and not writable by group or others."""
    try: st = os.stat(path)
    except OSError: return False
    return st.st_uid == 0 and not (st.st_mode & 0o022)

def safe_path_dirs():
    """$PATH entries that are absolute (an empty or relative entry means 'the current directory')."""
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)]

def which_safe(cmd):
    return shutil.which(cmd, path=os.pathsep.join(safe_path_dirs()))

def trusted_command(cmd):
    """Absolute path of the program `cmd` would run, but only when it is a system program: found on an absolute
    $PATH entry, and the file, its directory and the $PATH entry are all root-owned and not group/world
    writable.  Anything else (a relative path, '.' on $PATH, a directory you can write to) gives None."""
    if not cmd or "/" in cmd: return None
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            if os.path.exists(os.path.join(d or ".", cmd)): return None      # this is what would run, and it isn't trusted
            continue
        path = os.path.join(d, cmd)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            real = os.path.realpath(path)
            ok = _root_safe(d) and _root_safe(os.path.dirname(real)) and _root_safe(real)
            return path if ok else None
    return None

_where = ""            # "script.psh:12: " while running a script file, for error messages
_batch = False         # running a script / -c: an uncaught Python exception ends the run (interactive: it doesn't)
_errexit = False       # pysh -e: a failing command ends the run

def sh_error(msg): sys.stderr.write(f"pysh: {_where}{clean(str(msg))}\n"); sys.stderr.flush()

def _same_dir(a, b):
    try: return os.path.samefile(a, b)
    except OSError: return False

def logical_cwd():
    """$PWD if it really names the current directory (symlinks kept), else the physical path."""
    cwd, pwd = os.getcwd(), os.environ.get("PWD")
    return pwd if pwd and os.path.isabs(pwd) and _same_dir(pwd, cwd) else cwd

def fix_pwd():
    """Make $PWD agree with the real working directory.  A shell launched from elsewhere (wsl.exe from a
    Windows directory, su, sudo, a terminal that sets PWD itself) can inherit a stale or foreign PWD."""
    os.environ["PWD"] = logical_cwd()

def b_cd(a):
    physical = bool(a) and a[0] == "-P"
    if a and a[0] in ("-L", "-P"): a = a[1:]
    d = a[0] if a else os.path.expanduser("~")
    if d == "-": d = os.environ.get("OLDPWD", ".")
    old = logical_cwd()
    target = os.path.normpath(os.path.join(old, d)) if not physical else d    # ".." is resolved logically, as in bash
    try: os.chdir(target)
    except OSError:
        try: os.chdir(d)
        except OSError as e: sh_error(f"cd: {d}: {e.strerror}"); return 1
        target = d
    os.environ["OLDPWD"] = old
    new = os.getcwd()
    os.environ["PWD"] = target if (not physical and os.path.isabs(target) and _same_dir(target, new)) else new
    return 0
def b_pwd(a):
    print(os.getcwd() if a and a[0] == "-P" else logical_cwd())
    return 0
def b_exit(a):
    global _exit_warned
    if any(j.state == "stopped" for j in jobs) and not _exit_warned:
        _exit_warned = True; sh_error("there are stopped jobs (exit again to leave)"); return 1
    raise SystemExit(int(a[0]) if a else last.returncode)
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
Usage: pysh [-e] [-c COMMAND [ARG...]] [SCRIPT [ARG...]]
       pysh [-h | --help | -V | --version]"""

HELP = USAGE + """

pysh is a shell and a Python REPL in one: each line is run as Python if it
parses as Python (and doesn't start with an unbound command name), and as a
shell command otherwise.

Options:
  -c COMMAND     run COMMAND, then exit
  -e             stop at the first command that fails (scripts and -c)
  -h, --help     show this help and exit
  -V, --version  show version information and exit
  SCRIPT         run a file of mixed shell/Python lines; start it with `#!/usr/bin/pysh`
                 (or `#!/usr/bin/env pysh`) and `chmod +x` to run it directly.  An uncaught
                 Python exception ends the script with status 1.

Python <-> shell:
  $(cmd)     stdout of cmd, as a str          x = $(date +%Y)
  !(cmd)     a Result: .returncode .out .err .duration
  @(expr)    splice a Python value into a command    ls @(files)
  $VAR       environment variable; `$VAR = "x"` sets it
  ![cmd]     a Process you read while it runs: for line in ![tail -f log]: ...
  $[cmd]     run attached to the terminal (live, interactive programs work); returns a Result
  sh(cmd, tee=, check=, timeout=, stream=, capture=)   the same with options
  % cmd      a leading % forces the line to be a shell command (e.g. `% X=1; make`)
  last       the Result of the previous command
  aliases    dict: aliases['ll'] = 'ls -l', or a function(args) -> str|int

Shell syntax: pipes |  |&  && || ; &  redirects > >> < 2> 2>&1 >&2 &>  quoting,
globs, ~ and $VAR expansion, VAR=x cmd prefixes, {a,b} {1..5} braces,
$((1 + 2 * $X)) arithmetic, here-documents (<<EOF, <<-EOF, <<'EOF') and
here-strings (<<< word).

Job control: Ctrl-Z stops the foreground job; `jobs`, `fg [%n]`, `bg [%n]`,
`kill [-SIG] %n|pid`, `wait`.  `cmd &` runs in the background.

Builtins: """ + "{builtins}" + """

Editing keys:
  Right / End / Ctrl-F   accept the grey autosuggestion (Alt-F: one word)
  Up / Down              search history for entries starting with what you typed
  Ctrl-R                 history search menu (Enter inserts, Esc cancels)
  Tab / Shift-Tab        complete; again to cycle the menu (Enter runs it)
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

BUILTINS = {"cd": b_cd, "pwd": b_pwd, "exit": b_exit, "quit": b_exit, "export": b_export, "unset": b_unset,
            "alias": b_alias, "unalias": b_unalias, "source": b_source,
            ".": b_source, "history": b_history, "type": b_type, "help": b_help}

def open_redirs(st, env):
    """-> (stdin, stdout, stderr, to_close).  Files, or None for 'inherit'."""
    fds = {0: None, 1: None, 2: None}; close = []
    for fd, mode, tgt in st.redirs:
        if mode == "dup":
            if fd == 2 and tgt == 1: fds[2] = subprocess.STDOUT
            elif fd == 1 and tgt == 2: fds[1] = fds[2] if fds[2] not in (None, subprocess.STDOUT) else 2
            else: fds[fd] = fds.get(tgt)
            continue
        if mode in ("heredoc", "herestr"):
            if mode == "heredoc": body, quoted = tgt; text = body if quoted else expand_text(body)
            else: text = " ".join(expand_word(tgt)) + "\n"
            f = tempfile.TemporaryFile(); f.write(text.encode()); f.seek(0)
            close.append(f); fds[fd] = f
            continue
        names = expand_word(tgt)
        if len(names) != 1: raise SyntaxError("ambiguous redirect")
        f = open(names[0], {"<": "rb", ">": "wb", ">>": "ab"}[mode]); close.append(f)
        if fd == "both": fds[1] = fds[2] = f
        else: fds[fd] = f
    return fds[0], fds[1], fds[2], close

# ------------------------------------------------------------- job control
INTERACTIVE = False         # we own the terminal and manage process groups
SHELL_PGID = None
SHELL_MODE = None           # the shell's sane terminal modes, restored after each foreground job
jobs = []
_exit_warned = False

class Job:
    def __init__(self, pids, pgid, cmd):
        self.pids, self.live, self.pgid, self.cmd = list(pids), set(pids), pgid, cmd
        self.state, self.rc, self.id, self.notified, self.mode = "running", 0, 0, True, None

def add_job(job):
    used = {j.id for j in jobs}; n = 1
    while n in used: n += 1
    job.id = n; jobs.append(job)

def _note(job, pid, status):
    if os.WIFSTOPPED(status): job.state = "stopped"; job.notified = False; return
    if os.WIFCONTINUED(status): job.state = "running"; return
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
    job.live.discard(pid)
    if pid == job.pids[-1]: job.rc = rc
    if not job.live: job.state = "done"

def wait_job(job):
    """Block until every process of the job has exited (True) or the job stops (False)."""
    job.state = "running"
    for pid in list(job.pids):
        if pid not in job.live: continue
        while True:
            try: _, status = os.waitpid(pid, os.WUNTRACED); break
            except KeyboardInterrupt: continue
            except ChildProcessError: job.live.discard(pid); status = None; break
        if status is None: continue
        _note(job, pid, status)
        if job.state == "stopped":
            for q in job.live - {pid}:                  # collect the other stages' stop reports
                try: os.waitpid(q, os.WNOHANG | os.WUNTRACED)
                except OSError: pass
            return False
    job.state = "done"
    return True

def _tty(fn, *a):
    if not INTERACTIVE: return None
    try: return fn(*a)
    except OSError: return None

def run_foreground(job, resume=False):
    import _pyshline
    if job.pgid is not None:
        _tty(os.tcsetpgrp, 0, job.pgid)
        if resume:
            if job.mode: _tty(_pyshline.setmode, job.mode)
            try: os.killpg(job.pgid, signal.SIGCONT)
            except OSError: pass
    try: done = wait_job(job)
    finally:
        if job.state == "stopped" and INTERACTIVE:
            try: job.mode = _pyshline.getmode()
            except OSError: pass
        _tty(os.tcsetpgrp, 0, SHELL_PGID)
        if SHELL_MODE: _tty(_pyshline.setmode, SHELL_MODE)
    if done:
        if job in jobs: jobs.remove(job)
        return job.rc
    if job not in jobs: add_job(job)
    job.notified = True
    print(f"\n[{job.id}]+  Stopped                 {job.cmd}")
    return 148

def reap_jobs(notify=False):
    for job in list(jobs):
        for pid in list(job.live):
            try: r, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
            except ChildProcessError: job.live.discard(pid); continue
            if r: _note(job, pid, status)
        if not job.live: job.state = "done"
    if notify:
        for job in list(jobs):
            if job.state == "done":
                print(f"[{job.id}]  {'Done' if job.rc == 0 else 'Exit ' + str(job.rc):<22} {job.cmd}"); jobs.remove(job)
            elif job.state == "stopped" and not job.notified:
                job.notified = True; print(f"[{job.id}]+  Stopped                 {job.cmd}")

def hup_jobs():
    for job in jobs:
        if job.pgid is None: continue
        for s in (signal.SIGHUP, signal.SIGCONT):
            try: os.killpg(job.pgid, s)
            except OSError: pass

def init_job_control():
    """Become the foreground process group leader of our terminal (interactive shells only)."""
    global INTERACTIVE, SHELL_PGID, SHELL_MODE
    import _pyshline
    try:
        while os.tcgetpgrp(0) != os.getpgrp(): os.killpg(os.getpgrp(), signal.SIGTTIN)
        for s in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU): signal.signal(s, signal.SIG_IGN)
        try: os.setpgid(0, 0)
        except OSError: pass
        SHELL_PGID = os.getpgrp()
        os.tcsetpgrp(0, SHELL_PGID)
        SHELL_MODE = _pyshline.getmode()
        INTERACTIVE = True
    except OSError: INTERACTIVE = False

def _find_job(a):
    if a:
        try: n = int(a[0].lstrip("%"))
        except ValueError: sh_error(f"{a[0]}: no such job"); return None
        for j in jobs:
            if j.id == n: return j
        sh_error(f"{a[0]}: no such job"); return None
    live = [j for j in jobs if j.state != "done"]
    if not live: sh_error("no current job"); return None
    return next((j for j in reversed(live) if j.state == "stopped"), live[-1])

def b_fg(a):
    j = _find_job(a)
    if j is None: return 1
    print(j.cmd); sys.stdout.flush()
    return run_foreground(j, resume=True)
def b_bg(a):
    j = _find_job(a)
    if j is None: return 1
    if j.state == "stopped":
        try: os.killpg(j.pgid, signal.SIGCONT)
        except OSError as e: sh_error(f"bg: {e.strerror}"); return 1
        j.state = "running"
    print(f"[{j.id}]+ {j.cmd} &")
    return 0
def b_jobs(a):
    reap_jobs()
    cur = _find_job([]) if jobs else None
    for j in list(jobs):
        mark = "+" if j is cur else " "
        state = {"running": "Running", "stopped": "Stopped", "done": "Done"}[j.state]
        print(f"[{j.id}]{mark}  {state:<22} {j.cmd}")
        j.notified = True
        if j.state == "done": jobs.remove(j)
    return 0
def b_wait(a):
    targets = [_find_job([x]) for x in a] if a else list(jobs)
    rc = 0
    for j in targets:
        if j is None: return 127
        wait_job(j); rc = j.rc
        if j in jobs and j.state == "done": jobs.remove(j)
    return rc
def b_kill(a):
    sig, args = signal.SIGTERM, list(a)
    if args and args[0] == "-l":
        print(" ".join(s.name[3:] for s in sorted(signal.Signals, key=lambda s: s.value))); return 0
    if args and args[0] == "-s" and len(args) > 1: spec = args[1]; args = args[2:]
    elif args and args[0].startswith("-") and len(args[0]) > 1: spec = args[0][1:]; args = args[1:]
    else: spec = None
    if spec is not None:
        try: sig = signal.Signals(int(spec)) if spec.isdigit() else signal.Signals["SIG" + spec.upper().removeprefix("SIG")]
        except KeyError: sh_error(f"kill: {spec}: invalid signal"); return 1
    if not args: sh_error("kill: usage: kill [-SIGNAL] pid|%job ..."); return 2
    rc = 0
    for t in args:
        try:
            if t.startswith("%"):
                j = _find_job([t])
                if j is None: rc = 1; continue
                os.killpg(j.pgid, sig) if j.pgid else [os.kill(x, sig) for x in j.live]
                if sig != signal.SIGCONT and j.state == "stopped" and sig in (signal.SIGTERM, signal.SIGHUP): os.killpg(j.pgid, signal.SIGCONT)
            else: os.kill(int(t), sig)
        except (OSError, ValueError) as e: sh_error(f"kill: {t}: {getattr(e, 'strerror', None) or e}"); rc = 1
    return rc

BUILTINS.update(fg=b_fg, bg=b_bg, jobs=b_jobs, wait=b_wait, kill=b_kill)

def _child_setup_fg():
    try: os.tcsetpgrp(0, os.getpgrp())          # while SIGTTOU is still ignored
    except OSError: pass
    for s in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU): signal.signal(s, signal.SIG_DFL)
def _child_setup_bg():
    for s in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU): signal.signal(s, signal.SIG_DFL)

class _Killer:
    """Ends a command `timeout` seconds after it started: SIGTERM, then SIGKILL two seconds later."""
    def __init__(self, timeout, pids, pgid=None):
        self.fired, self.pids, self.pgid, self.t2 = False, list(pids), pgid, None
        self.t = threading.Timer(timeout, self.fire); self.t.daemon = True; self.t.start()
    def _sig(self, s):
        try:
            if self.pgid: os.killpg(self.pgid, s)
            else:
                for pid in self.pids:
                    try: os.kill(pid, s)
                    except OSError: pass
        except OSError: pass
    def fire(self):
        self.fired = True; self._sig(signal.SIGTERM)
        self.t2 = threading.Timer(2.0, lambda: self._sig(signal.SIGKILL)); self.t2.daemon = True; self.t2.start()
    def cancel(self):
        self.t.cancel()
        if self.t2: self.t2.cancel()

def _status(rc): return rc if rc >= 0 else 128 - rc

class Process:
    """A command that is still running, read as its output arrives:

        for line in ![tail -f /var/log/syslog]: ...
        with ![make -j8] as p: ...  (terminated on leaving the block if still running)

    Iterating yields lines without the newline.  .wait() returns the exit status; .result is a Result."""

    def __init__(self, procs, cmd, timeout=None, preset=None, rc_override=None, check=False, pgid=None):
        self.cmd, self.check, self._procs = cmd, check, [p for p in procs if p is not None]
        self._last = procs[-1] if procs and procs[-1] is not None else None
        self.pid = self._last.pid if self._last else (self._procs[-1].pid if self._procs else None)
        self._text = (io.StringIO(preset) if preset is not None else
                      io.TextIOWrapper(self._last.stdout, encoding="utf-8", errors="replace", newline="\n")
                      if self._last is not None and self._last.stdout is not None else None)
        self._rc, self._rc_override, self._t0 = None, rc_override, time.time()
        self._killer = _Killer(timeout, [q.pid for q in self._procs], pgid) if timeout and self._procs else None

    def readline(self): return self._text.readline() if self._text else ""
    def read(self): return self._text.read() if self._text else ""
    def __iter__(self): return self
    def __next__(self):
        line = self.readline()
        if line == "":
            self.wait(); raise StopIteration
        return line[:-1] if line.endswith("\n") else line

    def poll(self):
        if self._rc is None:
            if self._rc_override is not None and not self._procs: self._rc = self._rc_override
            elif all(q.poll() is not None for q in self._procs):
                self._rc = _status(self._last.returncode) if self._last else (self._rc_override or 127)
        return self._rc

    def wait(self, timeout=None):
        if self._rc is None:
            try:
                for q in self._procs: q.wait(timeout)
            except subprocess.TimeoutExpired: raise
            self.poll()
            if self._killer: self._killer.cancel()
            if self._rc_override is not None: self._rc = self._rc_override
        if self.check and self._rc != 0: raise ShellError(self.result)
        return self._rc

    returncode = property(poll)
    timed_out = property(lambda self: bool(self._killer and self._killer.fired))
    @property
    def result(self):
        r = Result(self._rc if self._rc is not None else self.poll(), None, None, time.time() - self._t0, self.cmd)
        r.timed_out = self.timed_out
        return r
    def _signal(self, s):
        for q in self._procs:
            try: q.send_signal(s)
            except OSError: pass
    def terminate(self): self._signal(signal.SIGTERM)
    def kill(self): self._signal(signal.SIGKILL)
    def send_signal(self, s): self._signal(s)
    def close(self):
        if self._text:
            try: self._text.close()
            except Exception: pass
    def __enter__(self): return self
    def __exit__(self, *exc):
        if self.poll() is None:
            self.terminate()
            try: self.wait(3)
            except subprocess.TimeoutExpired: self.kill(); self.wait()
        self.close(); return False
    def __del__(self): self.close()
    def __repr__(self):
        st = "running" if self.poll() is None else f"exited {self._rc}"
        return f"<Process {self.pid} {self.cmd!r} {st}>"

class ShellError(Exception):
    """Raised by sh(cmd, check=True) when the command exits non-zero."""
    def __init__(self, result):
        self.result, self.returncode = result, result.returncode
        super().__init__(f"command {result.cmd!r} exited with status {result.returncode}")

def _collect(p, tee):
    """Read a process's stdout (and stderr, if piped) to the end, echoing live when `tee`."""
    import codecs
    bufs = {"out": [], "err": []}
    def pump(f, name, sink):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = os.read(f.fileno(), 65536)
            if not chunk: break
            text = dec.decode(chunk); bufs[name].append(text)
            if sink is not None: sink.write(text); sink.flush()
        tail = dec.decode(b"", True)
        if tail: bufs[name].append(tail)
    t = None
    if p.stderr is not None:
        t = threading.Thread(target=pump, args=(p.stderr, "err", sys.stderr if tee else None), daemon=True); t.start()
    if p.stdout is not None: pump(p.stdout, "out", sys.stdout if tee else None)
    if t: t.join()
    p.wait()
    return ("".join(bufs["out"]) if p.stdout is not None else None,
            "".join(bufs["err"]) if p.stderr is not None else None)

def run_pipeline(pipe, capture=False, capture_err=False, background=False, cmdline="", tee=False, stream=False,
                 timeout=None, merge=False):
    sys.stdout.flush(); sys.stderr.flush()
    t0 = time.time()
    argvs = []
    for st in pipe:
        words, env = list(st.words), {}
        while words and len(words[0]) and words[0][0][0] == "lit" and re.match(r"[A-Za-z_]\w*=", words[0][0][1]):
            k, _, v = words[0][0][1].partition("=")
            words[0] = [("lit", v)] + words[0][1:] if (v or len(words[0]) > 1) else [("sq", "")]
            vals = expand_word(words[0])
            env[k] = vals[0] if vals else ""
            words.pop(0)
        argv = [a for w in words for a in expand_word(w)]
        argvs.append((argv, env))
    if len(pipe) == 1 and not argvs[0][0]:           # bare VAR=val
        os.environ.update(argvs[0][1]); return Result(0, "", "", 0, cmdline)

    def inproc_fn(argv, env=None):
        if argv[0] in BUILTINS: fn = lambda: BUILTINS[argv[0]](argv[1:])
        else:
            f = aliases.get(argv[0])
            if not callable(f): return None
            fn = lambda: f(argv[1:])
        if not env: return fn
        def with_env():                                   # FOO=x builtin: visible only while it runs
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try: return fn()
            finally:
                for k, v in saved.items():
                    if os.environ.get(k) != env[k]: continue      # the builtin changed it itself; keep that
                    if v is None: os.environ.pop(k, None)
                    else: os.environ[k] = v
        return with_env

    if len(pipe) == 1 and not pipe[0].redirs and not background:
        fn = inproc_fn(*argvs[0])
        if fn:
            rc, out = run_inproc(fn, capture)
            return Result(rc, out, "", time.time() - t0, cmdline)

    use_jobs = background or (INTERACTIVE and not capture and not capture_err and not stream and not tee)
    procs, prev, close, pgid = [], None, [], None
    tail_rc = tail_out = None            # outcome of a final stage that ran in-process / was not found
    for n, (st, (argv, env)) in enumerate(zip(pipe, argvs)):
        last_stage = n == len(pipe) - 1
        if not argv: raise SyntaxError("empty command")
        try:
            rin, rout, rerr, c = open_redirs(st, env); close += c
        except OSError as e:
            for f in close: f.close()
            sh_error(f"{e.filename}: {e.strerror}"); return Result(1, "", "", 0, cmdline)
        fn = inproc_fn(argv, env) if (len(pipe) > 1 or st.redirs) else None
        if fn:                                            # builtin / python alias as a pipeline stage
            src = rin if rin is not None else prev
            text = src.read().decode(errors="replace") if src is not None else None
            if prev is not None: prev.close(); prev = None
            rc, o = run_inproc(fn, True, text)
            data = (o or "").encode()
            if rout is not None:
                (os.write(rout, data) if isinstance(rout, int) else (rout.write(data), rout.flush()))
            elif last_stage:
                if capture: tail_out = o
                else: sys.stdout.write(o or ""); sys.stdout.flush()
            else:
                tf = tempfile.TemporaryFile(); tf.write(data); tf.seek(0); prev = tf
            procs.append(None)
            if last_stage: tail_rc = rc
            continue
        stdout = rout if rout is not None else (subprocess.PIPE if (not last_stage or capture or stream or tee) else None)
        stderr = rerr if rerr is not None else (subprocess.STDOUT if (st.merge_err or (merge and last_stage)) else
                                                subprocess.PIPE if ((capture_err or tee) and last_stage) else None)
        stdin = rin if rin is not None else prev
        if stdin is None and background and not INTERACTIVE: stdin = subprocess.DEVNULL
        kw = {}
        if use_jobs or timeout:                       # a timeout needs its own process group, to kill grandchildren too
            kw["process_group"] = 0 if pgid is None else pgid
            if use_jobs: kw["preexec_fn"] = _child_setup_fg if (INTERACTIVE and not background) else _child_setup_bg if INTERACTIVE else None
        try:
            p = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr,
                                 env={**os.environ, **env} if env else None, **kw)
        except (FileNotFoundError, PermissionError) as e:
            sh_error(f"{argv[0]}: {'command not found' if isinstance(e, FileNotFoundError) else 'permission denied'}")
            if INTERACTIVE and not background: _tty(os.tcsetpgrp, 0, SHELL_PGID)   # the failed child had already grabbed the terminal
            procs.append(None); prev = None
            if last_stage: tail_rc = 127
            continue
        if use_jobs or timeout:
            if pgid is None: pgid = p.pid
            try: os.setpgid(p.pid, pgid)
            except OSError: pass
        if prev is not None and prev is not rin: prev.close()
        prev = p.stdout if (stdout is subprocess.PIPE and not last_stage) else None
        procs.append(p)
    for f in close: f.close()

    live = [p for p in procs if p is not None]
    if stream:
        return Process(procs, cmdline, timeout, tail_out, tail_rc, pgid=pgid)
    killer = _Killer(timeout, [p.pid for p in live], pgid) if timeout and live else None
    if use_jobs and live:
        job = Job([p.pid for p in live], pgid, cmdline)
        for p in live: p.returncode = 0                   # we reap these ourselves; keep Popen from doing so
        if background:
            add_job(job)
            if INTERACTIVE: print(f"[{job.id}] {job.pids[-1]}")
            return Result(0, None, None, 0, cmdline)
        rc = run_foreground(job)
        if tail_rc is not None: rc = tail_rc
        res = Result(rc, tail_out, None, time.time() - t0, cmdline)
        if killer: killer.cancel(); res.timed_out = killer.fired
        return res
    if background: return Result(0, None, None, 0, cmdline)

    out = err = None; rc = 127
    for n, p in enumerate(procs):
        if p is None: continue
        while True:
            try:
                if n == len(procs) - 1 and (capture or capture_err or tee):
                    out, err = _collect(p, tee)
                else: p.wait()
                break
            except KeyboardInterrupt: pass
        rc = _status(p.returncode)
    if tail_rc is not None: rc = tail_rc
    if tail_out is not None: out = tail_out
    res = Result(rc, out, err, time.time() - t0, cmdline)
    if killer: killer.cancel(); res.timed_out = killer.fired
    return res

def run_inproc(fn, capture, stdin_text=None):
    """Run a builtin / python alias, optionally capturing its stdout and feeding it stdin."""
    buf = io.StringIO() if capture else None
    old, old_in = sys.stdout, sys.stdin
    if capture: sys.stdout = buf
    if stdin_text is not None: sys.stdin = io.StringIO(stdin_text)
    try: r = fn()
    except SystemExit: raise
    except Exception as e: sh_error(f"{type(e).__name__}: {e}"); r = 1
    finally: sys.stdout, sys.stdin = old, old_in
    if isinstance(r, str):
        if capture: buf.write(r if r.endswith("\n") else r + "\n")
        else: sys.stdout.write(r if r.endswith("\n") else r + "\n")
        r = 0
    return (r if isinstance(r, int) else 0), (buf.getvalue() if capture else None)

def expand_alias(line):
    m = re.match(r"\s*((?:[A-Za-z_]\w*=(?:\"[^\"]*\"|'[^']*'|[^\s|&;<>])*\s+)*)([^\s|&;<>]+)", line)   # skip leading FOO=bar words
    if m and isinstance(aliases.get(m.group(2)), str):
        return line[:m.start(2)] + aliases[m.group(2)] + line[m.end(2):]
    return line

def run_shell(line, capture=False, capture_err=False, tee=False, stream=False, timeout=None, merge=False):
    global last
    line = line.lstrip()
    if line.startswith("%"): line = line[1:]              # `% cmd` forces shell mode
    line, bodies, _ = split_heredocs(line)
    line = expand_alias(line)
    try: items = parse(line, bodies)
    except SyntaxError as e: sh_error(f"syntax error: {e.msg if hasattr(e,'msg') and e.msg else e}"); _setlast(Result(2, cmd=line)); return last
    if stream and (len(items) != 1 or items[0][1] == "&"):
        raise ValueError("![...] streams one pipeline; use sh(cmd) or a plain command for lists and background jobs")
    res, skip_until = Result(0, "" if capture else None, cmd=line), None
    for pipe, term in items:
        if skip_until == "&&" and res.returncode != 0 or skip_until == "||" and res.returncode == 0:
            skip_until = term if term in ("&&", "||") else None
            continue
        skip_until = None
        try:
            res = run_pipeline(pipe, capture and not term == "&", capture_err, term == "&", line, tee, stream, timeout, merge)
            if stream and isinstance(res, Process):
                _setlast(Result(0, None, None, 0, line)); return res
        except SyntaxError as e: sh_error(f"syntax error: {e}"); res = Result(2, cmd=line)
        except OSError as e: sh_error(str(e)); res = Result(1, cmd=line)
        except ArithmeticError as e: sh_error(str(e)); res = Result(1, cmd=line)
        _setlast(res)
        if _errexit and res.returncode != 0 and term not in ("&&", "||", "&"): raise SystemExit(res.returncode)
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
            j = match_paren(s, i + 1); inner = s[i+2:j-1]
            out.append(f"__arith__({inner[1:-1]!r})" if is_arith(inner) else f"__sh_out__({inner!r})"); i = j
        elif c in "!$" and s[i+1:i+2] == "[":
            j = match_bracket(s, i + 1)
            out.append(f"{'__stream__' if c == '!' else '__live__'}({s[i+2:j-1]!r})"); i = j
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

_tls = threading.local()

def _scope():
    """(globals, locals) that @(expr) is evaluated in: the Python frame that ran the command."""
    return getattr(_tls, "scope", None) or (ns, ns)

def _scoped(fn):
    def wrapper(c, *a, **kw):
        f = sys._getframe(1)
        old = getattr(_tls, "scope", None)
        _tls.scope = (f.f_globals, f.f_locals)
        try: return fn(c, *a, **kw)
        finally: _tls.scope = old
    return wrapper

def sh(cmd, capture=True, tee=False, check=False, timeout=None, stream=False, merge=False):
    """Run a shell command from Python.
         capture=True   collect stdout and stderr into the Result (default); False lets them reach the terminal
         tee=True       collect them *and* show them live
         check=True     raise ShellError if the command exits non-zero
         timeout=N      stop the command after N seconds (SIGTERM, then SIGKILL) and raise subprocess.TimeoutExpired
         stream=True    return a Process to read as output arrives (stderr stays on the terminal unless merge=True)
       Equivalent syntax: !(cmd) = sh(cmd), ![cmd] = sh(cmd, stream=True), $[cmd] = sh(cmd, capture=False)."""
    if stream:
        proc = run_shell(cmd, stream=True, timeout=timeout, merge=merge)
        proc.check = check
        return proc
    live = not capture and not tee
    r = run_shell(cmd, capture=not live, capture_err=not live, tee=tee, timeout=timeout)
    if r.timed_out: raise subprocess.TimeoutExpired(cmd, timeout, output=r.out, stderr=r.err)
    if check and r.returncode != 0: raise ShellError(r)
    return r

ns.update(shell=shell, last=last, __arith__=arith, __env__=_Env(), aliases=aliases, Result=Result,
          __sh__=_scoped(lambda c: run_shell(c, capture=True, capture_err=True)),
          __sh_out__=_scoped(lambda c: capture(c).rstrip("\n")),
          __stream__=_scoped(lambda c: sh(c, stream=True)),
          __live__=_scoped(lambda c: sh(c, capture=False)),
          sh=_scoped(sh), ShellError=ShellError, Process=Process)
ns["__doc__"] = None
ns["import_cwd"] = True                          # `import` finds modules in the current directory (or a script's own directory); False disables
ns["probe_flags"] = True                         # Tab on a -flag runs `cmd --help` for system programs; False disables
ns["complete_ignore_dirs"] = ("/mnt/c",)   # PATH directories hidden from command completion
ns["complete_ignore"] = (".dll", ".exe", ".vbs", ".efi", ".mof", ".bat", ".drv", ".sys", ".tlb", ".xml")   # command-name suffixes hidden from Tab completion (set in ~/.pyshrc.py)

KW = {"def", "class", "if", "elif", "else", "for", "while", "with", "try", "except", "finally", "import",
      "from", "async", "return", "raise", "del", "assert", "global", "nonlocal", "pass", "lambda", "yield", "match", "await"}

def classify(line):
    s = line.strip()
    m = re.match(r"[^\s|&;<>()=]+", s)
    w = m.group() if m else ""
    if s.startswith("%"): return "sh"                  # `% cmd`: shell, whatever it looks like
    kw = re.match(r"[A-Za-z_]\w*", s)
    if (kw and kw.group() in KW and not (kw.group() in BUILTINS)) or s.startswith("@"): return "py"
    if w in BUILTINS and w not in ns and not s[len(w):].lstrip().startswith(("(", "=", ".", "[")): return "sh"
    try: py = preprocess(s)
    except SyntaxError: return "sh"                    # e.g. an unbalanced $( : the shell parser reports it
    try: code = codeop.compile_command(py, "<pysh>", "single")
    except (SyntaxError, ValueError, OverflowError): return "sh"
    if code is None:                                  # unfinished: an open bracket/string, or a block header
        return "sh" if (w and w not in ns and is_command(w)) else "py"
    try: tree = ast.parse(py)
    except SyntaxError: return "sh"
    if tree.body and all(isinstance(x, ast.Expr) for x in tree.body) and w:
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
            sh_error(f"{e.name}: command not found"); _setlast(Result(127, cmd=src)); return
        traceback.print_exception(type(e), e, e.__traceback__.tb_next)
        _setlast(Result(1, cmd=src)); return
    except BaseException as e:
        tb = e.__traceback__.tb_next if e.__traceback__ and not isinstance(e, SyntaxError) else None
        traceback.print_exception(type(e), e, tb)
        _setlast(Result(1, cmd=src))
        return
    _setlast(Result(0, cmd=src))

def run_line(line):
    """Run one logical (possibly multi-physical-line) entry."""
    if classify(line.split("\n", 1)[0]) == "sh": return run_shell(line)
    return run_python(line)

_CONT = re.compile(r"\s*(else|elif|except|finally)\b")

def _needs_more(src):
    """True if `src` is an unfinished Python statement (open bracket, string, or block header)."""
    try: compile(preprocess(src) + "\n", "<pysh>", "exec"); return False
    except SyntaxError as e:
        return any(x in str(e.msg) for x in ("never closed", "unterminated triple", "expected an indented block", "unexpected EOF"))
    except (ValueError, OverflowError): return False

def run_script(text, filename="<pysh>"):
    """Run a file of mixed shell/Python lines.  A Python block ends at the first line that isn't indented
    (or a blank line followed by one); shell lines run one at a time (with here-document bodies)."""
    global _where
    lines, i = text.splitlines(), 0
    if lines and lines[0].startswith("#!"): lines[0] = ""                      # keep numbering, drop the shebang
    isfile = not filename.startswith("<")
    try:
        while i < len(lines):
            ln = lines[i]; start = i + 1; i += 1
            if not ln.strip() or ln.lstrip().startswith("#"): continue
            buf = [ln]
            if isfile: _where = f"{os.path.basename(filename)}:{start}: "
            if classify(ln) == "sh":
                while buf[-1].endswith("\\") and i < len(lines): buf[-1] = buf[-1][:-1] + lines[i]; i += 1
                while not split_heredocs("\n".join(buf))[2] and i < len(lines): buf.append(lines[i]); i += 1
            else:
                while i < len(lines):
                    if _needs_more("\n".join(buf)) or buf[-1].lstrip().startswith("@"):
                        buf.append(lines[i]); i += 1; continue
                    if _CONT.match(lines[i]) and re.match(r"\s*(try|if|for|while)\b", buf[0]):     # one-line `try: x` then `except:`
                        buf.append(lines[i]); i += 1; continue
                    if buf[0].rstrip().endswith(":") or len(buf) > 1:
                        j = i
                        while j < len(lines) and not lines[j].strip(): j += 1
                        if j < len(lines) and (lines[j][:1] in (" ", "\t") or _CONT.match(lines[j])):
                            buf.extend(lines[i:j+1]); i = j + 1; continue
                    break
            run_py_or_sh("\n".join(buf), filename, start)
    finally:
        _where = ""

def run_py_or_sh(src, filename="<pysh>", lineno=1):
    first = src.split("\n", 1)[0]
    if classify(first) == "sh" and ("\n" not in src or heredoc_markers(first)): return run_shell(src)
    src = src if src.endswith("\n") else src + "\n"
    try:
        exec(compile("\n" * (lineno - 1) + preprocess(src), filename, "exec"), ns)    # padding keeps real line numbers
    except SystemExit: raise
    except NameError as e:
        word = src.strip()
        if re.fullmatch(r"[\w.+-]+", word) and e.name == word:                  # a lone unknown word is a missing command
            sh_error(f"{word}: command not found"); _setlast(Result(127, cmd=word))
            if _errexit: raise SystemExit(127)
            return
        traceback.print_exception(type(e), e, e.__traceback__.tb_next)
        if _batch: raise SystemExit(1)
    except BaseException as e:
        traceback.print_exception(type(e), e, e.__traceback__.tb_next if not isinstance(e, SyntaxError) else None)
        if _batch: raise SystemExit(1)

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
    return clean(head[16:] if head.startswith("ref: refs/heads/") else head[:7]) if head else None

def prompt():
    _cmdcache.clear()
    cwd = logical_cwd(); home = os.path.expanduser("~")
    if cwd == home or cwd.startswith(home + "/"): cwd = "~" + cwd[len(home):]
    short = cwd                                 # the full path, never abbreviated
    rc = f"\x1b[31m[{last.returncode}]\x1b[0m" if last.returncode else ""
    b = git_branch()
    br = f"\x1b[37m({b})\x1b[0m" if b else ""
    user = clean(os.environ.get("USER") or "user")
    host = clean(os.uname().nodename.split(".")[0])
    short = clean(short)
    return f"\x1b[32m{user}@{host}\x1b[0m \x1b[36m{short}\x1b[0m{br}{rc}\x1b[90m>\x1b[0m "

# ------------------------------------------------- fish-style autosuggest/highlight
def _valid_cmd(w):
    return is_command(w) if "/" not in w else os.access(os.path.expanduser(w), os.X_OK)

def suggest(buf):
    if not buf.strip() or "\n" in buf: return ""
    for h in reversed(history):
        if h.startswith(buf) and len(h) > len(buf) and "\n" not in h: return clean(h[len(buf):])
    return ""

_SHTOK = re.compile(r"""(?P<ws>\s+)|(?P<str>'[^']*'?|"(?:\\.|[^"\\])*"?)|(?P<op>\|\||&&|>>|[0-9]?>&[0-9]|[|;&<>])
                        |(?P<cm>\#.*)|(?P<w>(?:\\.|[^\s|&;<>'"\\])+)|(?P<x>.)""", re.X)
_PYTOK = re.compile(r"""(?P<str>(?:[rbfRBF]{0,2})(?:\"\"\"[\s\S]*?(?:\"\"\"|$)|'[^'\n]*'?|"(?:\\.|[^"\\\n])*"?))|(?P<cm>\#.*)|(?P<n>\b\d[\w.]*)|(?P<w>[A-Za-z_]\w*)|(?P<x>[\s\S])""", re.X)
C = lambda n, t: f"\x1b[{n}m{t}\x1b[0m"

def highlight(buf):
    buf = _CTRL.sub("?", buf)                 # same width, so the cursor math still holds
    m = re.match(r"(\s*)%", buf)
    if m: return m.group(1) + C(90, "%") + highlight_sh(buf[m.end():])
    try: kind = classify(buf) if buf.strip() else "sh"
    except Exception: kind = "sh"
    out = []
    w = buf.strip()
    if kind == "py" and re.fullmatch(r"[A-Za-z_][\w.+-]*", w) and w not in ns and not hasattr(builtins, w) and not keyword.iskeyword(w):
        kind = "sh"                       # a bare word that is neither a variable nor builtin: judge it as a command
    if kind == "py":
        for m in _PYTOK.finditer(buf):
            k, t = m.lastgroup, m.group()
            if k == "str": out.append(C(33, t))
            elif k == "cm": out.append(C(90, t))
            elif k == "n": out.append(C(36, t))
            elif k == "w" and keyword.iskeyword(t): out.append(C(34, t))
            elif k == "w" and t in ns and callable(ns[t]) or k == "w" and hasattr(builtins, t) and callable(getattr(builtins, t)): out.append(C(36, t))
            else: out.append(t)
        return "".join(out)
    return highlight_sh(buf)

def highlight_sh(buf):
    out = []
    cmd = True
    for m in _SHTOK.finditer(buf):
        k, t = m.lastgroup, m.group()
        if k == "w":
            if cmd and re.match(r"[A-Za-z_]\w*=", t): out.append(t); continue
            if cmd:
                out.append(t); cmd = False                          # the program name keeps the default colour, like its arguments
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
    """Parse `cmd --help` into [(flag, description)] (cached, 1.5s timeout).

    This runs the program, so it is limited to system programs (see trusted_command) and can be turned off
    with `probe_flags = False` in ~/.pyshrc.py."""
    if not ns.get("probe_flags", True): return []
    if cmd in _helpcache: return _helpcache[cmd]
    res, path = [], trusted_command(cmd)
    if path:
        try:
            r = subprocess.run([path, "--help"], stdin=subprocess.DEVNULL, capture_output=True, cwd="/",
                               timeout=1.5, text=True, errors="replace")
            seen = set()
            for line in (r.stdout or r.stderr).splitlines():
                t = line.strip()
                if not t.startswith("-"): continue
                parts = re.split(r"\s{2,}", t, 1)
                spec, desc = parts[0], (parts[1] if len(parts) > 1 else "")
                for f in re.findall(r"(?:^|[\s,|])(--?[A-Za-z0-9][\w-]*)", spec):
                    if f not in seen: seen.add(f); res.append((f, clean(desc.strip()[:70])))
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
            whatis = which_safe("whatis")
            if not whatis: return _mandb
            r = subprocess.run([whatis, "-l", "-s", "1,6,8", "-r", ".*"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=3, text=True, errors="replace", cwd="/",
                               env={**os.environ, "LC_ALL": "C"})
            for line in r.stdout.splitlines():
                m = re.match(r"(\S+)\s+\((\w+)\)\s+- (.*)", line)
                if m and (m.group(1) not in _mandb or m.group(2) == "1"): _mandb[m.group(1)] = clean(m.group(3))
        except Exception: pass
    return _mandb

BUILTIN_DOC = {"cd": "change directory", "exit": "leave the shell", "quit": "leave the shell",
               "export": "set environment variables", "unset": "remove environment variables",
               "alias": "define or list aliases", "unalias": "remove aliases", "jobs": "list background jobs",
               "source": "run a script in this shell", ".": "run a script in this shell",
               "history": "show command history", "pwd": "print the working directory", "fg": "resume a job in the foreground", "bg": "resume a job in the background",
               "wait": "wait for background jobs", "kill": "signal a process or %job", "type": "describe how a name resolves", "help": "pysh help"}

def _pydoc(o):
    d = (getattr(o, "__doc__", None) or "").strip().split("\n")[0] if not isinstance(o, (int, str, float, list, dict, tuple, set)) else ""
    return d[:70]

def _human(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T": return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024

def _esc(w): return re.sub(r"([ \t'\"\\$&;|<>()*?#])", r"\\\1", w)

_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")

def _dotted(base):
    """Look up `a.b.c` without evaluating anything else (no calls, no indexing) and without running properties."""
    if not _DOTTED.fullmatch(base): raise ValueError(base)
    first, *rest = base.split(".")
    obj = ns[first] if first in ns else getattr(builtins, first)
    for name in rest:
        try:
            attr = inspect.getattr_static(obj, name)
            if isinstance(attr, property): raise ValueError(name)       # would run user code
            obj = getattr(obj, name) if not isinstance(attr, (types.ModuleType, type)) else attr
        except AttributeError:
            obj = getattr(obj, name)                                    # dynamic attribute (__getattr__)
    return obj

def complete(before):
    """-> (text_before_cursor_with_common_prefix, [(full_text, display, description)])"""
    forced = re.match(r"\s*%", before)
    if forced:                                   # `% cmd`: complete as a shell command; keep the % in the results
        k = forced.end() - 1
        text, items = complete(before[:k] + " " + before[k+1:])
        fix = lambda s: s[:k] + "%" + s[k+1:]
        return fix(text), [(fix(f), d, e) for f, d, e in items]
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
            obj = _dotted(base); nospace = True
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
        st = os.stat(HIST)
        if st.st_uid == os.getuid() and st.st_mode & 0o077: os.chmod(HIST, 0o600)       # it may hold secrets
        with open(HIST) as f: history.extend(l.rstrip("\n") for l in f if l.strip())
    except OSError: pass

def save_history(line):
    if line.startswith(" "): return                 # like bash's ignorespace: a leading space keeps a command out of history
    if line.strip() and (not history or history[-1] != line):
        history.append(line)
        try:
            fd = os.open(HIST, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f: f.write(line + "\n")
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
    if INTERACTIVE: reap_jobs(notify=True)
    p = ns.get("prompt", prompt)
    first = rl(p() if callable(p) else p)
    if not first.strip(): return first
    lines = [first]
    while True:
        src = "\n".join(lines)
        if src.endswith("\\"): lines[-1] = lines[-1][:-1]; lines.append(rl("\x1b[90m…\x1b[0m ")); continue
        if classify(lines[0]) != "py":
            if split_heredocs(src)[2]: break                         # here-document still open?
            lines.append(rl("\x1b[90m…\x1b[0m ")); continue
        try: code = codeop.compile_command(preprocess(src), "<pysh>", "single")
        except SyntaxError: break
        if code is not None and not (len(lines) > 1 and lines[-1].strip()): break
        if code is not None and len(lines) == 1 and not first.rstrip().endswith(":"): break
        lines.append(rl("\x1b[90m…\x1b[0m "))
    return "\n".join(lines)

def default_aliases():
    """Interactive niceties: `ls` gets --color=auto when this ls supports it (checked once at startup).
    Override or remove in ~/.pyshrc.py (aliases["ls"] = ... / del aliases["ls"])."""
    ls = which_safe("ls")                     # never a './ls' picked up through a relative PATH entry
    if not ls: return
    try:
        ok = subprocess.run([ls, "--color=auto", "-d", "."], stdin=subprocess.DEVNULL, cwd="/",
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0
    except Exception: ok = False
    if ok: aliases.setdefault("ls", "ls --color=auto")

def repl():
    tty = os.isatty(0) and os.isatty(1)
    global _exit_warned
    if tty:
        init_job_control()
        default_aliases()
        load_history()
        rcf = os.path.expanduser("~/.pyshrc.py")
        if os.path.exists(rcf):
            try: exec(compile(open(rcf).read(), rcf, "exec"), ns)
            except Exception: traceback.print_exc()
        drop_import_cwd()
    internal_errors = 0
    while True:
        try: line = read_entry(tty)
        except EOFError:
            if any(j.state == "stopped" for j in jobs) and not _exit_warned:
                _exit_warned = True; print(); sh_error("there are stopped jobs (^D again to leave)"); continue
            break
        except KeyboardInterrupt: continue
        except Exception as e:                                     # a bug must not log you out
            internal_errors += 1
            sh_error(f"internal error while reading input: {type(e).__name__}: {e}")
            if internal_errors > 20: return 70
            continue
        if not line.strip(): continue
        if tty: save_history(line)
        _exit_warned = False if not line.strip().startswith(("exit", "quit")) else _exit_warned
        try: run_line(line)
        except SystemExit as e: hup_jobs(); return exit_status(e)
        except KeyboardInterrupt: print()
        except Exception as e:
            sh_error(f"internal error: {type(e).__name__}: {e}"); _setlast(Result(70, cmd=line))
    if tty: print()
    hup_jobs()
    return last.returncode

def setup_import_path(first):
    """Like python: the REPL, -c and piped input search the current directory ('' follows `cd`); a script file
    searches its own directory. PYSH_IMPORT_CWD=0 turns it off here; `import_cwd = False` in ~/.pyshrc.py does too."""
    if os.environ.get("PYSH_IMPORT_CWD", "1") in ("0", "false", "no", ""): return
    if first not in sys.path: sys.path.insert(0, first)

def drop_import_cwd():
    """After ~/.pyshrc.py: honour `import_cwd = False` by removing the current directory from the search path."""
    if not ns.get("import_cwd", True):
        while "" in sys.path: sys.path.remove("")

def exit_status(e):
    """Exit status for a SystemExit, as Python does: None is 0, an int is itself, anything else is printed to stderr and is 1."""
    c = e.code
    if c is None: return 0
    if isinstance(c, int): return c
    try: sys.stdout.flush()
    except Exception: pass
    print(c, file=sys.stderr)
    return 1

def main(argv):
    global _batch, _errexit
    fix_pwd()
    args, i = list(argv[1:]), 0
    while i < len(args) and args[i].startswith("-") and args[i] not in ("-",):
        a = args[i]
        if a == "--": i += 1; break
        if a in ("-V", "--version"):
            print(f"pysh {VERSION}\nPython {sys.version}\nhome {sys.prefix}"); return 0
        if a in ("-h", "--help"): print(HELP.replace("{builtins}", " ".join(sorted(BUILTINS)))); return 0
        if a == "-e": _errexit = True; i += 1; continue
        if a == "-c": break
        sh_error(f"unknown option {a}"); print(USAGE, file=sys.stderr); return 2
    args = args[i:]
    if args and args[0] == "-c":
        if len(args) < 2: sh_error("-c needs an argument"); return 2
        sys.argv, _batch = ["pysh"] + args[2:], True
        setup_import_path("")
        try: run_script(args[1])
        except SystemExit as e: return exit_status(e)
        return last.returncode
    if args:
        path = args[0]
        sys.argv, _batch = args, True
        setup_import_path(os.path.dirname(os.path.realpath(path)))
        try:
            with open(path) as f: text = f.read()
        except OSError as e: sh_error(f"{path}: {e.strerror}"); return 127
        ns["__file__"] = os.path.abspath(path)
        try: run_script(text, path)
        except SystemExit as e: return exit_status(e)
        return last.returncode
    setup_import_path("")
    return repl()

if __name__ == "__main__":
    sys.stdout.flush()
    raise SystemExit(main(sys.argv))

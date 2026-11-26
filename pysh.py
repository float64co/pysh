# pysh -- embedded shell logic.  Executed by pysh.c inside __main__.
import ast, builtins, codeop, collections, collections.abc, contextlib, glob, importlib, importlib.abc, importlib.util, json, inspect, io, keyword, operator, os, re, shlex, shutil, signal, site, subprocess, sys, tempfile, threading, time, traceback, types, warnings

VERSION = "0.0.3"
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
        self.value = None            # what a final Python stage returned
        self.no_match = False        # status 1 only because a search found nothing: -e carries on
        self._statuses = None
    @property
    def statuses(self):
        """the exit status of every stage of the pipeline"""
        return list(self._statuses) if self._statuses is not None else [self.returncode]
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

# --------------------------------------------------------------- pyshlib
cd_hooks = []        # functions run after `cd` (pyshlib.on_cd)
completers = {}      # command -> function(word, args) (pyshlib.completer)

class _PyshlibImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """`import pyshlib`: the copy built into the binary. It comes first on the meta path, so a pyshlib.py
    lying in the current directory is never picked up instead."""
    def find_spec(self, name, path=None, target=None):
        return importlib.util.spec_from_loader(name, self) if name == "pyshlib" else None
    def exec_module(self, module):
        module.__dict__["_shell"] = shell
        exec(compile(globals()["__pyshlib__"], "<pyshlib>", "exec"), module.__dict__)

sys.meta_path.insert(0, _PyshlibImporter())

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

_AT_CALL = re.compile(r"@[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(")

def _is_name_equals(segs):
    """the word so far is exactly `name=`, so `name=@fn(...)` follows"""
    return len(segs) == 1 and segs[0][0] == "lit" and re.fullmatch(r"[A-Za-z_]\w*=", segs[0][1]) is not None

def _closing(s, i):
    """index just past the bracket matching s[i], if what follows can end a word; else None"""
    try: j = match_paren(s, i)
    except SyntaxError: return None
    return j if s[j:j+1] in ("", " ", "\t", "\n", "|", "&", ";", "<", ">") else None

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
        elif c == "@" and (cur is None or _is_name_equals(cur)) and (m := _AT_CALL.match(s, i)) and (j := _closing(s, m.end() - 1)):
            add("call", s[i+1:j]); i = j                    # `@where(...)`: one word, spaces and all
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

def expand_assignment(segs):
    """The value of `NAME=...`, as bash does it: no word splitting, no globbing, no braces,
    and `~` is expanded at the start and after `=` or `:`  (so `PATH=~/bin:~/x:$PATH` works)."""
    return _expand_word(segs, assign=True)[0]

def _expand_word(segs, assign=False):
    fields = [[]]                 # each field: list of (text, quoted)
    def put(t, q): fields[-1].append((t, q))
    for idx, (kind, text) in enumerate(segs):
        if kind == "lit":
            if idx == 0 and text.startswith("~"):
                m = re.match(r"~[^/:]*" if assign else r"~[^/]*", text); text = os.path.expanduser(m.group()) + text[m.end():]
            if assign: text = re.sub(r"(?<=[=:])~[^/:=]*", lambda m: os.path.expanduser(m.group()), text)
            put(text, False)
        elif kind == "sq": put(text, True)
        elif kind == "call": put("@" + text, True)          # not in a command position: just the text
        elif kind == "dq":
            put("", True)
            for k, t in expand_dq(text):
                if k == "sq": put(t, True)
                elif k == "var": put(getvar(t), True)
                elif k == "arith": put(str(arith(t)), True)
                else: put(capture(t).rstrip("\n"), True)
        elif assign and kind == "var": put(getvar(text), True)
        elif assign and kind == "sub": put(capture(text).rstrip("\n"), True)
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
            if assign: vs = [" ".join(map(str, vs))]
            for j, x in enumerate(vs):
                if j or not fields[-1]: pass
                if j: fields.append([])
                put(str(x), True)
    if assign: return ["".join(t for t, _ in fields[0])]
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

def expand_aliases(toks, used=None):
    """Replace a string alias wherever a command starts: the beginning of the line and after ; & && || | |&
    (leading `NAME=value` words and redirections don't count). Quoted or escaped names (`\\ls`, `'ls'`) are
    left alone, and an alias is never expanded again inside its own expansion (`alias ls='ls --color'`).
    As in bash, a value ending in a blank makes the following word a candidate too (`alias sudo='sudo '`)."""
    out, queue = [], [(t, frozenset()) for t in toks]
    at_command, is_target = True, False
    while queue:
        t, inside = queue.pop(0)
        if t is None: at_command = True                     # end of an alias that ended in a blank
        elif t[0] == "w":
            if is_target: is_target = False
            elif at_command:
                kind, text = t[1][0]
                if kind == "lit" and re.match(r"[A-Za-z_]\w*=", text): pass          # VAR=x cmd
                elif len(t[1]) == 1 and kind == "lit" and text not in inside and isinstance(aliases.get(text), str):
                    value = aliases[text]
                    if used is not None: used.append(text)
                    more = [(None, inside)] if value.endswith((" ", "\t")) else []
                    queue[:0] = [(x, inside | {text}) for x in tokenize(value)] + more
                    continue
                else: at_command = False
        elif t[0] == "redir": is_target = True
        elif t[0] == "op": at_command = True
        if t is not None: out.append(t)
    return out

def parse(line, heredocs=None, used_aliases=None):
    """-> list of (pipeline:list[Stage], terminator: ';' '&&' '||' '&' '')"""
    heredocs = iter(heredocs or [])
    toks = expand_aliases(tokenize(line), used_aliases)
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
class _CommandIndex:
    """Which names are on $PATH, kept in memory, so judging a half-typed line doesn't search every directory
    on each keystroke (one search costs ~50 ms when $PATH has Windows directories under WSL).

    Interactive shells only. A background thread lists the directories at startup. Before each prompt, a
    directory that is quick to `stat` is re-listed if it changed; a slow one is re-listed in the background
    once a minute. The index can be out of date, so it only ever *speeds up* an answer: a hit is checked on
    disk, and when a line is run, a miss is confirmed with a real search."""
    SLOW_STAT = 0.0005              # seconds; a directory slower than this to stat is refreshed in the background
    SLOW_AGE = 60.0                 # how stale a slow directory's listing may get

    def __init__(self):
        self.dirs = {}              # directory -> (names, mtime_ns, slow, listed_at)
        self.started = False
        self._pending = set()

    @staticmethod
    def path():
        return list(dict.fromkeys(os.environ.get("PATH", os.defpath).split(os.pathsep)))

    def _stat(self, d):
        t = time.perf_counter()
        try: mtime = os.stat(d).st_mtime_ns
        except OSError: mtime = None
        return mtime, time.perf_counter() - t > self.SLOW_STAT

    def _scan(self, d, known=None):
        mtime, slow = known or self._stat(d)
        try: names = frozenset(os.listdir(d)) if mtime is not None else frozenset()
        except OSError: names = frozenset()
        self.dirs[d] = (names, mtime, slow, time.monotonic())

    def _background(self, directories):
        todo = [d for d in directories if d not in self._pending]
        if not todo: return
        self._pending.update(todo)
        def work():
            for d in todo:
                try: self._scan(d)
                finally: self._pending.discard(d)
        threading.Thread(target=work, daemon=True, name="pysh-path-index").start()

    def start(self):
        self.started = True
        self._background([d for d in self.path() if os.path.isabs(d)])

    def refresh(self):
        """called before each prompt; must stay quick"""
        if not self.started: return
        later, now = [], time.monotonic()
        for d in self.path():
            if not os.path.isabs(d): continue
            entry = self.dirs.get(d)
            if entry is None:                              # new on $PATH: list it now if it is quick
                known = self._stat(d)
                if known[1]: later.append(d)
                else: self._scan(d, known)
            elif entry[2]:                                 # slow: never make the prompt wait for it
                if now - entry[3] > self.SLOW_AGE: later.append(d)
            else:
                mtime, slow = self._stat(d)
                if mtime != entry[1]: self._scan(d, (mtime, slow))
        self._background(later)

    def lookup(self, name):
        """is there an executable `name` in a $PATH directory, as far as the index knows?"""
        for d in self.path():
            entry = self.dirs.get(d)
            if os.path.isabs(d) and (entry is None or name not in entry[0]): continue
            full = os.path.join(d or ".", name)                    # relative entries depend on the cwd: look directly
            if os.access(full, os.X_OK) and not os.path.isdir(full): return True
        return False

    def names(self, d):
        """the listing of `d` if the index has one, else None"""
        entry = self.dirs.get(d) if self.started else None
        return None if entry is None else entry[0]

command_index = _CommandIndex()
_typing = False             # the line editor is asking: answer from the index alone

def on_path(w):
    if not command_index.started or "/" in w or not w: return shutil.which(w) is not None
    if command_index.lookup(w): return True
    return False if _typing else shutil.which(w) is not None       # about to be run: make sure before saying no

_cmdcache = {}          # name -> bool; cleared before every prompt so new installs are noticed
def is_command(w):
    if w in BUILTINS or w in aliases: return True
    r = _cmdcache.get(w)
    if r is None:
        r = on_path(w)
        if r or not _typing: _cmdcache[w] = r           # a "no" from the index alone is not remembered
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
_at_prompt = False     # running a line typed at the prompt (so a returned value goes to `_`)
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
    run_cd_hooks()
    return 0
_in_cd_hook = False

def run_cd_hooks():
    global _in_cd_hook
    if _in_cd_hook or not cd_hooks: return                  # a hook that cds doesn't start them again
    _in_cd_hook, before = True, last
    try:
        for hook in list(cd_hooks):
            try: hook(logical_cwd())
            except SystemExit: raise
            except Exception as e: sh_error(f"on_cd {getattr(hook, '__name__', hook)}: {type(e).__name__}: {e}")
    finally:
        _in_cd_hook = False
        _setlast(before)

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
# ----------------------------------------------------------- virtualenvs
def _from_stdlib(p):
    stdlib = os.path.dirname(os.__file__)
    return p.startswith(stdlib) and "-packages" not in p or p.endswith(".zip") and os.path.dirname(p) == os.path.dirname(stdlib)

# what `site` added for the system interpreter; an isolated virtualenv hides these
_SITE_ENTRIES = [p for p in sys.path if p and not _from_stdlib(p) and p not in os.environ.get("PYTHONPATH", "").split(os.pathsep)]
_venv = None                 # what activating changed, so deactivate can undo it

def _venv_config(root):
    """pyvenv.cfg as a dict, or None if `root` is not a virtualenv."""
    try:
        with open(os.path.join(root, "pyvenv.cfg")) as f:
            return {k.strip().lower(): v.strip() for k, sep, v in (line.partition("=") for line in f) if sep}
    except (OSError, UnicodeDecodeError):
        return None

def _forget_modules(roots):
    """drop cached modules loaded from under any of `roots`, so the next import looks again"""
    roots = tuple(r.rstrip("/") + "/" for r in roots)
    for name, mod in list(sys.modules.items()):
        if (getattr(mod, "__file__", None) or "").startswith(roots) and name != "__main__": del sys.modules[name]

def _venv_imports(root, cfg):
    """Point `import` at the virtualenv. -> the saved state, or None if it was made for another Python version."""
    want = (cfg.get("version") or cfg.get("version_info") or "").split(".")[:2]
    if want != [str(sys.version_info[0]), str(sys.version_info[1])]: return None
    packages = os.path.join(root, "lib", f"python{want[0]}.{want[1]}", "site-packages")
    isolated = cfg.get("include-system-site-packages", "false").lower() != "true"
    before = list(sys.path)
    site.addsitedir(packages)                             # also processes .pth files
    added = [p for p in sys.path if p not in before]
    hidden = [p for p in _SITE_ENTRIES if p in before and p not in added] if isolated else []
    rest = [p for p in before if p not in hidden]
    at = next((i for i, p in enumerate(rest) if p in _SITE_ENTRIES), len(rest))        # ahead of system packages
    sys.path[:] = rest[:at] + added + rest[at:]
    state = {"added": added, "hidden": hidden, "prefix": (sys.prefix, sys.exec_prefix), "executable": sys.executable}
    sys.prefix = sys.exec_prefix = root
    sys.executable = os.path.join(root, "bin", "python")
    _forget_modules(hidden)
    importlib.invalidate_caches()
    return state

def activate_venv(root):
    """Use the virtualenv at `root`: $PATH and $VIRTUAL_ENV for commands, sys.path for `import`."""
    global _venv
    root = os.path.abspath(os.path.expanduser(root))
    cfg = _venv_config(root)
    if cfg is None or not os.path.isdir(os.path.join(root, "bin")):
        sh_error(f"activate: {root}: not a virtualenv (no pyvenv.cfg and bin/)"); return 1
    if os.environ.get("VIRTUAL_ENV"): deactivate_venv()
    pythonhome = os.environ.pop("PYTHONHOME", None)
    os.environ["VIRTUAL_ENV"] = root
    os.environ["PATH"] = os.path.join(root, "bin") + os.pathsep + os.environ.get("PATH", os.defpath)
    state = _venv_imports(root, cfg)
    if state is None:
        sh_error(f"activate: {root} is a Python {cfg.get('version', '?')} virtualenv and pysh runs "
                 f"{sys.version_info[0]}.{sys.version_info[1]}: its commands are on $PATH, but `import` here is unchanged")
        state = {}
    _venv = dict(state, root=root, pythonhome=pythonhome)
    _cmdcache.clear()
    return 0

def deactivate_venv():
    global _venv
    root = os.environ.pop("VIRTUAL_ENV", None)
    if root is None: sh_error("deactivate: no virtualenv is active"); return 1
    bin_dir = os.path.join(root, "bin")                   # take out only our entry; keep other $PATH changes
    os.environ["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != bin_dir)
    state, _venv = _venv or {}, None
    if state.get("pythonhome") is not None: os.environ["PYTHONHOME"] = state["pythonhome"]
    if "added" in state:
        sys.path[:] = [p for p in sys.path if p not in state["added"]] + [p for p in state["hidden"] if p not in sys.path]
        sys.prefix, sys.exec_prefix = state["prefix"]
        sys.executable = state["executable"]
        _forget_modules([root])
        importlib.invalidate_caches()
    _cmdcache.clear()
    return 0

def adopt_venv():
    """Started inside an activated virtualenv ($VIRTUAL_ENV): make `import` see it, as `python` would."""
    global _venv
    root = os.environ.get("VIRTUAL_ENV")
    cfg = _venv_config(root) if root and os.path.isabs(root) else None
    if cfg is None: return
    state = _venv_imports(root, cfg)
    if state is not None: _venv = dict(state, root=root, pythonhome=None)

def b_activate(a):
    if len(a) > 1: sh_error("usage: activate [DIR]"); return 2
    if a: return activate_venv(a[0])
    for guess in (".venv", "venv"):
        if _venv_config(guess) is not None: return activate_venv(guess)
    sh_error("activate: no .venv or venv here; give the directory"); return 1

def b_deactivate(a): return deactivate_venv()

def b_source(a):
    if not a: sh_error("source: filename required"); return 2
    path = os.path.abspath(a[0])
    if os.path.basename(path) == "activate" and os.path.basename(os.path.dirname(path)) == "bin":
        root = os.path.dirname(os.path.dirname(path))
        if _venv_config(root) is not None: return activate_venv(root)      # that file is bash; do what it means
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
Usage: pysh [-eil] [--norc] [-c COMMAND [ARG...]] [SCRIPT [ARG...]]
       pysh [-h | --help | -V | --version]"""

HELP = USAGE + """

pysh is a shell and a Python REPL in one: each line is run as Python if it
parses as Python (and doesn't start with an unbound command name), and as a
shell command otherwise.

Options:
  -c COMMAND     run COMMAND, then exit
  -e             stop at the first command that fails (scripts and -c)
  -i             interactive: read ~/.pyshrc, also before -c or a script
  -l, --login    accepted for login(1), ssh, tmux and editors; no extra file is read
  --norc         don't read (or create) ~/.pyshrc; use it to get in if that file is broken
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
  cmd | @fn          a Python stage: fn gets a text stream         ... | @json.load
  cmd | x=@fn        keep fn's result in x          x=@ keeps the raw text, x=@tee also shows it
  set_pipefail = True    make a pipeline fail when any stage fails
  quiet_failures = True  no Python traceback when an earlier command in the pipeline already failed
  explain 'line'         how a line would be read, without running it
  bash_env FILE          run FILE in bash and take over its environment changes (-n: only show them)
  from pyshlib import *  extras: @table @freq @stats @where(f) ..., par(), hist, on_cd, completer
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
  Alt-E                  edit the line in $EDITOR (default vim); save and quit to run it
  Tab / Shift-Tab        complete; again to cycle the menu (Enter runs it)
  Ctrl-A/E/K/U/W/L/D     start, end, kill to end, kill line, kill word, clear, delete
  Alt-B / Alt-F          move by word

Files:
  ~/.pyshrc              run at interactive startup like a script; created if missing
  ~/.pysh_history        command history ($PYSH_HISTFILE to change)

Environment: PYSH_HOME (Python home; default is the build-time prefix),
PYTHONHOME. See pysh(1) for details."""

def b_help(a):
    print(HELP.replace("{builtins}", " ".join(sorted(BUILTINS))))
    return 0

BUILTINS = {"cd": b_cd, "pwd": b_pwd, "exit": b_exit, "quit": b_exit, "export": b_export, "unset": b_unset,
            "alias": b_alias, "unalias": b_unalias, "source": b_source,
            ".": b_source, "activate": b_activate, "deactivate": b_deactivate, "history": b_history, "type": b_type, "help": b_help}

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

def _pipe_status(statuses):
    """A pipeline's exit status: the last stage's; with `set_pipefail`, the last failure of any stage.
    A stage killed by SIGPIPE only because a later one stopped reading doesn't count."""
    if not statuses: return 0
    if statuses[-1] or not ns.get("set_pipefail"): return statuses[-1]
    return next((s for s in reversed(statuses) if s not in (0, 128 + signal.SIGPIPE)), 0)

class Job:
    def __init__(self, pids, pgid, cmd):
        self.pids, self.live, self.pgid, self.cmd = list(pids), set(pids), pgid, cmd
        self.state, self.rc, self.id, self.notified, self.mode = "running", 0, 0, True, None
        self.codes = {}                       # pid -> exit status

def add_job(job):
    used = {j.id for j in jobs}; n = 1
    while n in used: n += 1
    job.id = n; jobs.append(job)

def _note(job, pid, status):
    if os.WIFSTOPPED(status): job.state = "stopped"; job.notified = False; return
    if os.WIFCONTINUED(status): job.state = "running"; return
    rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
    job.live.discard(pid)
    job.codes[pid] = rc
    if not job.live:
        job.state = "done"
        job.rc = _pipe_status([job.codes.get(p, 0) for p in job.pids])

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
                self._rc = (_pipe_status([_status(q.returncode) for q in self._procs]) if self._last
                            else (self._rc_override or 127))
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
    nodes = plan_stages(pipe)
    if nodes is not None:
        return run_python_pipeline(nodes, capture, capture_err, background, cmdline, tee, stream, timeout, merge)
    argvs = []
    for st in pipe:
        words, env = list(st.words), {}
        while words and len(words[0]) and words[0][0][0] == "lit" and re.match(r"[A-Za-z_]\w*=", words[0][0][1]):
            k, _, v = words[0][0][1].partition("=")
            words[0] = [("lit", v)] + words[0][1:] if (v or len(words[0]) > 1) else [("sq", "")]
            env[k] = expand_assignment(words[0])
            words.pop(0)
        argv = []
        for w in words:
            if argv[:1] == ["export"] and w and w[0][0] == "lit" and re.match(r"[A-Za-z_]\w*=", w[0][1]):
                argv.append(expand_assignment(w))                 # `export NAME=value` is an assignment too
            else: argv.extend(expand_word(w))
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
    own_rc = {}                          # stage number -> status, for stages with no process
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
            procs.append(None); own_rc[n] = rc
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
            procs.append(None); prev = None; own_rc[n] = 127
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
        codes = None
        if job.state == "done":
            codes = [own_rc.get(n, 0) if p is None else job.codes.get(p.pid, 0) for n, p in enumerate(procs)]
            rc = _pipe_status(codes)
        res = Result(rc, tail_out, None, time.time() - t0, cmdline)
        res._statuses = codes
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
    codes = [own_rc.get(n, 0) if p is None else _status(p.returncode) for n, p in enumerate(procs)]
    if procs: rc = _pipe_status(codes)
    if tail_out is not None: out = tail_out
    res = Result(rc, out, err, time.time() - t0, cmdline)
    res._statuses = codes or None
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

# ------------------------------------------------------ Python in pipelines
# `@name` / `@(expr)` stages and `name=@...` captures (see the README, "Python in pipelines").  Only
# pipelines that contain one of these come here; everything else takes the path above.
_AT_NAME = re.compile(r"@([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\Z")
_CAPTURE = re.compile(r"([A-Za-z_]\w*)=@(.*)\Z", re.S)
_ENV_WORD = re.compile(r"[A-Za-z_]\w*=")

def lines(stream):
    """`... | files=@lines`: the lines of a stream, without newlines."""
    return stream.read().splitlines()

class _Node:
    """One stage.  kind: cmd (a program), call (Python), data (a value as a source), tap (capture / @tee)."""
    def __init__(self, stage, kind, **kw):
        self.stage, self.kind = stage, kind
        self.argv, self.env, self.fn, self.args, self.value = [], {}, None, [], None
        self.name = None                 # variable to assign
        self.show = False                # tap: also display
        self.source = False              # call without a stream
        self.label = ""
        self.__dict__.update(kw)
        self.proc, self.thread, self.status, self.data = None, None, 0, None
        self.assigned_ok = False
        self.defer, self.error = False, None      # quiet_failures: hold a traceback until we know why

def _env_words(stage, capture_context):
    """split leading NAME=value words off a stage -> (env, remaining words)"""
    words, env = list(stage.words), {}
    while words and words[0] and words[0][0][0] == "lit" and _ENV_WORD.match(words[0][0][1]):
        if capture_context and _is_capture_word(words[0]): break
        k, _, v = words[0][0][1].partition("=")
        words[0] = [("lit", v)] + words[0][1:] if (v or len(words[0]) > 1) else [("sq", "")]
        env[k] = expand_assignment(words[0])
        words.pop(0)
    return env, words

def _is_capture_word(w):
    """an unquoted `name=@`, `name=@dotted` or `name=@(expr)`"""
    if w[0][0] != "lit": return False
    if len(w) == 1: return bool(_CAPTURE.match(w[0][1]))
    return len(w) == 2 and w[1][0] in ("py", "call") and re.fullmatch(r"[A-Za-z_]\w*=", w[0][1]) is not None

def _has_input(stage): return any(fd == 0 and mode in ("<", "heredoc", "herestr") for fd, mode, _ in stage.redirs)
def _has_output(stage): return any(mode in (">", ">>") and fd in (1, "both") for fd, mode, _ in stage.redirs)

def _evaluate(expr):
    """eval a stage's `@(expr)`.  A lambda written there must see the caller's locals, and eval() gives it no
    closure, so inside a function the locals are merged into the globals it gets."""
    g, l = _scope()
    return eval(expr, g if l is g else {**g, **l})

def _lookup(name):
    g, l = _scope()
    first, *rest = name.split(".")
    if first in l: obj = l[first]
    elif first in g: obj = g[first]
    elif hasattr(builtins, first): obj = getattr(builtins, first)
    else: raise NameError(f"name '{first}' is not defined (import it first?)")
    for part in rest: obj = getattr(obj, part)
    return obj

def _words(words):
    out = []
    for w in words: out.extend(expand_word(w))
    return out

def plan_stages(pipe):
    """-> list of _Node if the pipeline involves Python or captures, else None.  `@(expr)` that turns out not
    to be a stage is rewritten in place to the words it splices to, so it is evaluated exactly once."""
    def interesting(st, i):
        words = st.words
        ctx = i > 0 or _has_input(st)
        k = 0
        while k < len(words) and words[k] and words[k][0][0] == "lit" and _ENV_WORD.match(words[k][0][1]):
            if ctx and _is_capture_word(words[k]): return True
            k += 1
        if k >= len(words): return False
        w = words[k]
        if len(w) != 1: return False
        kind, text = w[0]
        if kind == "lit": return bool(_AT_NAME.match(text))
        return kind in ("py", "call") and (len(pipe) > 1 or bool(st.redirs))
    if not any(interesting(st, i) for i, st in enumerate(pipe)): return None

    nodes, special = [], False
    for i, st in enumerate(pipe):
        last, ctx = i == len(pipe) - 1, i > 0 or _has_input(st)
        env, words = _env_words(st, ctx)
        if not words:
            if env and len(pipe) == 1: return None
            raise SyntaxError("empty command")
        w = words[0]
        first_kind, first_text = w[0]
        if ctx and _is_capture_word(w):
            if len(w) == 1: name, spec = _CAPTURE.match(first_text).groups()
            else: name, spec = first_text[:-1], None
            if name in KW or keyword.iskeyword(name): raise SyntaxError(f"cannot assign to {name}")
            if spec in ("", "tee"):
                if len(words) > 1: raise SyntaxError(f"{name}=@{spec} takes no arguments")
                nodes.append(_Node(st, "tap", name=name, show=spec == "tee", env=env))
            else:
                if not last: raise SyntaxError(f"{name}=@function must be the last stage")
                if spec is None: fn, label = _evaluate(w[1][1]), "@" + w[1][1] if w[1][0] == "call" else f"@({w[1][1]})"
                else:
                    if not _AT_NAME.match("@" + spec): raise SyntaxError(f"bad function name @{spec} (use @(...) for an expression)")
                    fn, label = _lookup(spec), "@" + spec
                if not callable(fn): raise TypeError(f"{label} is not callable")
                nodes.append(_Node(st, "call", name=name, fn=fn, label=label, args=_words(words[1:]), env=env))
            special = True
            continue
        single = len(w) == 1
        if single and first_kind == "lit" and first_text == "@tee":
            if len(words) > 1: raise SyntaxError("@tee takes no arguments")
            nodes.append(_Node(st, "tap", show=True, env=env)); special = True
            continue
        at = _AT_NAME.match(first_text) if single and first_kind == "lit" else None
        if (single and first_kind in ("py", "call") and i == 0 and len(words) == 1 and not _has_input(st)
                and (len(pipe) > 1 or _has_output(st))):
            # `@(...) | cmd`: always a source.  Evaluate it inside the stage, where print() reaches the pipe.
            label = "@" + first_text if first_kind == "call" else f"@({first_text})"
            nodes.append(_Node(st, "expr", expr=first_text, label=label, source=True, env=env)); special = True
            continue
        if at or (single and first_kind in ("py", "call") and (len(pipe) > 1 or st.redirs)):
            obj = _lookup(at.group(1)) if at else _evaluate(first_text)
            label = first_text if at else "@" + first_text if first_kind == "call" else f"@({first_text})"
            source = i == 0 and not _has_input(st)
            if callable(obj):
                nodes.append(_Node(st, "call", fn=obj, label=label, args=_words(words[1:]), source=source, env=env))
                special = True
                continue
            feeds = len(pipe) > 1 or _has_output(st)
            if source and len(words) == 1 and (at or feeds):
                nodes.append(_Node(st, "data", value=obj, label=label, env=env)); special = True
                continue
            if at: raise TypeError(f"{label} is not callable")
            if first_kind == "call": raise TypeError(f"{label} did not return a function")
            values = obj if isinstance(obj, (list, tuple, set)) else [obj]              # splice, as before
            words = [[("sq", str(v))] for v in values] + words[1:]
        argv = _words(words)
        if not argv: raise SyntaxError("empty command")
        st.words = [[("sq", a)] for a in argv]                      # already expanded: don't do it twice
        nodes.append(_Node(st, "cmd", argv=argv, env=env, label=argv[0]))
    if not special:
        for n, st in zip(nodes, pipe):                              # put NAME=value words back for the usual path
            st.words = [[("lit", f"{k}="), ("sq", v)] for k, v in n.env.items()] + st.words
        return None
    for node in nodes:                                             # builtins and function aliases are Python too
        if node.kind == "cmd" and (node.argv[0] in BUILTINS or callable(aliases.get(node.argv[0]))):
            node.kind, node.fn = "builtin", BUILTINS.get(node.argv[0]) or aliases[node.argv[0]]
            node.source = node is nodes[0] and not _has_input(node.stage)          # nothing to read: leave stdin alone
    return nodes

def _write_result(value, out):
    """Put what a stage returned on its output.  -> the value to keep (iterators become lists)."""
    def text(item):
        return item.decode(errors="replace") if isinstance(item, (bytes, bytearray)) else item if isinstance(item, str) else str(item)
    if value is None: return None
    if isinstance(value, (str, bytes, bytearray)): out.write(text(value))
    elif isinstance(value, (list, tuple, collections.abc.Iterator)):
        streaming, seen = isinstance(value, collections.abc.Iterator), []
        for item in value:
            s = text(item)
            out.write(s if s.endswith("\n") else s + "\n")
            if streaming:
                out.flush()
                if out.keeps: seen.append(item)
        if streaming: return seen if out.keeps else None       # consumed and not remembered: nothing to hand back
        return value
    else:
        s = str(value)
        if s: out.write(s if s.endswith("\n") else s + "\n")            # an empty result prints nothing, not a blank line
    return value

class _Out:
    """A stage's output stream; `keeps` says whether items of an iterator must be remembered."""
    def __init__(self, stream, keeps=False): self.stream, self.keeps = stream, keeps
    def write(self, s): return self.stream.write(s)
    def flush(self): self.stream.flush()

def _text_reader(fd):
    return io.TextIOWrapper(os.fdopen(fd, "rb"), encoding="utf-8", errors="replace", newline="\n")

def _call_hint(node, e):
    name = getattr(node.fn, "__name__", "")
    if isinstance(e, TypeError) and name.endswith("loads") and node.label.endswith("loads"):
        sh_error(f"hint: did you mean {node.label[:-1]}? The function gets a stream, not a string.")

def _run_function(node, in_fd, out):
    """Call a stage's function.  -> (status, value).  `in_fd` is consumed."""
    stream = _text_reader(in_fd) if in_fd is not None and not node.source else None
    if in_fd is not None and stream is None: os.close(in_fd)
    saved = {k: os.environ.get(k) for k in node.env}
    os.environ.update(node.env)
    try:
        with contextlib.redirect_stdout(out.stream):
            if node.kind == "builtin":
                text = stream.read() if stream else None
                rc, produced = run_inproc(lambda: node.fn(node.argv[1:]), True, text)
                out.write(produced or ""); return rc, None
            if node.kind == "data": value = node.value
            elif node.kind == "expr":
                value = _evaluate(node.expr)
                if callable(value): value = value()
            else: value = node.fn(*([] if stream is None else [stream]), *node.args)
            if node.name:
                if isinstance(value, collections.abc.Iterator): value = list(value)    # while the input is still open
                return 0, value
            ending = getattr(type(value), "_as_stage", None)     # a function's result that has its own output and status
            if ending and node.kind == "call": return ending(value, out), value
            return 0, _write_result(value, out)
    except (SystemExit, KeyboardInterrupt): raise
    except BrokenPipeError: raise
    except Exception as e:
        tb = e.__traceback__.tb_next
        if node.kind == "expr":                                    # skip _evaluate and the eval frame: show the user's code
            if tb is not None: tb = tb.tb_next
            while tb is not None and tb.tb_next is not None and tb.tb_frame.f_code.co_filename == "<string>" and tb.tb_frame.f_code.co_name == "<module>":
                tb = tb.tb_next
        if node.defer: node.error = (e, tb)
        else:
            traceback.print_exception(type(e), e, tb)
            _call_hint(node, e)
        return 1, None
    finally:
        for k, v in saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        if stream is not None:
            try: stream.close()                        # a writer upstream now gets SIGPIPE
            except OSError: pass

class _ForkProc:
    """A forked Python stage, with enough of Popen's interface for jobs, waiting and ![...]."""
    def __init__(self, pid): self.pid, self.returncode, self.stdout, self.stderr = pid, None, None, None
    def _reap(self, flags):
        if self.returncode is None:
            try: pid, status = os.waitpid(self.pid, flags)
            except ChildProcessError: self.returncode = 0; return self.returncode
            if pid: self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode
    def poll(self): return self._reap(os.WNOHANG)
    def wait(self, timeout=None):
        if timeout is None: return self._reap(0)
        end = time.time() + timeout
        while self.poll() is None:
            if time.time() >= end: raise subprocess.TimeoutExpired("python stage", timeout)
            time.sleep(0.01)
        return self.returncode
    def send_signal(self, s):
        if self.returncode is None: os.kill(self.pid, s)
    def terminate(self): self.send_signal(signal.SIGTERM)
    def kill(self): self.send_signal(signal.SIGKILL)

def _fd(x): return x if x is None or isinstance(x, int) else x.fileno()

def _fork_stage(node, fds, others, pgid, foreground):
    """Run a Python stage in a child.  fds = (in, out, err), None to inherit; `others` are pipe ends to close."""
    sys.stdout.flush(); sys.stderr.flush()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)           # "fork() in a multi-threaded process"
        pid = os.fork()
    if pid: return _ForkProc(pid)
    status = 1
    try:
        global INTERACTIVE
        if pgid is not None:
            try: os.setpgid(0, pgid)
            except OSError: pass
            if foreground and pgid == 0:
                try: os.tcsetpgrp(0, os.getpgrp())
                except OSError: pass
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT): signal.signal(s, signal.SIG_DFL)
        if INTERACTIVE:
            for s in (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU): signal.signal(s, signal.SIG_DFL)
        INTERACTIVE = False; jobs.clear()
        for target, source in enumerate(fds):
            if source is not None and source != target: os.dup2(source, target)
        for fd in set(others) | {f for f in fds if f is not None}:
            if fd > 2:
                try: os.close(fd)
                except OSError: pass
        sys.stdin = open(0, "r", errors="replace", closefd=False)
        sys.stdout = open(1, "w", errors="replace", closefd=False, buffering=1 if os.isatty(1) else -1)
        sys.stderr = open(2, "w", errors="replace", closefd=False, buffering=1)
        try:
            in_fd = None if node.source or node.kind == "data" else os.dup(0)
            status, _ = _run_function(node, in_fd, _Out(sys.stdout))
            sys.stdout.flush()
        except BrokenPipeError: status = 128 + signal.SIGPIPE
        except SystemExit as e: status = exit_status(e)
        except KeyboardInterrupt: status = 130
        try: sys.stderr.flush()
        except OSError: pass
    finally:
        os._exit(status)                                # never return into the shell's own code

def _tap(node, in_fd, out_fd, show_fd):
    """The shell copies a stage's data, keeping it (and showing it): name=@, name=@tee, @tee."""
    chunks = []
    def write(fd, data):
        view = memoryview(data)
        while view: view = view[os.write(fd, view):]
    try:
        while True:
            chunk = os.read(in_fd, 65536)
            if not chunk: break
            if node.name: chunks.append(chunk)
            if show_fd is not None:
                try: write(show_fd, chunk)
                except OSError: show_fd = None
            if out_fd is not None: write(out_fd, chunk)
    except BrokenPipeError: node.status = 128 + signal.SIGPIPE        # the next stage stopped reading
    except OSError as e: node.status = 1; sh_error(f"pipeline: {e.strerror}")
    finally:
        node.data = b"".join(chunks)
        for fd in (in_fd, out_fd):
            if fd is not None:
                try: os.close(fd)
                except OSError: pass

def _outcome(node, allowed):
    """"ok", "nomatch" (a search that found nothing) or "fail"."""
    s = node.status
    if s == 0 or s == 128 + signal.SIGPIPE: return "ok"          # SIGPIPE: a later stage stopped reading
    if s == 1 and node.kind == "cmd" and os.path.basename(node.argv[0]) in allowed: return "nomatch"
    return "fail"

def _report_deferred(nodes, target):
    """With quiet_failures, the last stage's error was held back. Show it only if nothing before it had failed."""
    final = nodes[-1]
    if final.error is None: return
    e, tb = final.error
    allowed = {os.path.basename(c) for c in ns.get("no_match_commands", ())}
    cause = next((n for n in nodes[:-1] if _outcome(n, allowed) == "fail"), None)
    if cause is None:
        with contextlib.redirect_stderr(target):
            traceback.print_exception(type(e), e, tb)
            _call_hint(final, e)
    elif not final.name:                                   # for name=@..., _judge says it all
        sh_error(f"{final.label}: no result, because {cause.label} exited with status {cause.status}")

def _judge(nodes):
    """Assign the captured variables that earned it.  -> (status, no_match)."""
    allowed = {os.path.basename(c) for c in ns.get("no_match_commands", ())}
    failed, no_match, empty_ok, assigned = None, False, False, False
    for node in nodes:
        if node.name:
            assigned = True
            ok = failed is None and (node.kind == "tap" or node.status == 0)
            if ok:
                ns[node.name] = node.value if node.kind == "call" else node.data.decode("utf-8", "replace")
                if no_match: empty_ok = True
            elif failed is not None:
                sh_error(f"{node.name} not assigned: {failed[0]} exited with status {failed[1]}")
            else: sh_error(f"{node.name} not assigned: {node.label} failed")
        outcome = _outcome(node, allowed)
        if outcome == "nomatch": no_match = True
        elif outcome == "fail" and failed is None: failed = (node.label or "a stage", node.status)
    last = nodes[-1].status
    if not assigned: return _pipe_status([n.status for n in nodes]), False
    if failed is not None: return failed[1], False
    if last: return last, False
    return (1, True) if empty_ok else (0, False)

def run_python_pipeline(nodes, capture, capture_err, background, cmdline, tee, stream, timeout, merge):
    t0 = time.time()
    captures = any(n.kind == "tap" or n.name for n in nodes)
    if captures and (background or stream):
        raise SyntaxError("captures and name=@... can't be used with & or ![...]")
    final = nodes[-1]
    # The last stage stays in the shell if it is Python or a capture, unless the shell must not block on it.
    here = final.kind not in ("cmd", "data", "expr") and not background and not stream
    threads_needed = any(n.kind == "tap" for n in nodes[:-1])
    use_jobs = background or (INTERACTIVE and not capture and not capture_err and not stream and not tee
                              and not here and not threads_needed)
    pipes, close_files, plumbing = [], [], []
    pgid, live, taps = None, [], []

    def new_pipe():
        r, w = os.pipe(); pipes.extend((r, w)); return r, w

    try:
        incoming, stream_out = None, None
        for i, node in enumerate(nodes):                 # first lay out every stage's descriptors
            last = i == len(nodes) - 1
            rin, rout, rerr, c = open_redirs(node.stage, node.env); close_files += c
            stdin = _fd(rin) if rin is not None else incoming
            if incoming is not None and rin is not None: os.close(incoming); pipes.remove(incoming)
            stdout, nxt = _fd(rout), None
            if not last:
                nxt, w = new_pipe()
                if stdout is None: stdout = w
                else: os.close(w); pipes.remove(w)       # output redirected: the next stage reads nothing
            elif stream and stdout is None:
                r, stdout = new_pipe(); stream_out = r
            stderr = stdout if rerr == subprocess.STDOUT else _fd(rerr)
            if node.stage.merge_err or (merge and last): stderr = stdout
            plumbing.append((node, stdin, stdout, stderr, last))
            incoming = nxt

        for node, stdin, stdout, stderr, last in plumbing:          # processes first, threads afterwards
            group = (0 if pgid is None else pgid) if (use_jobs or timeout) else None
            if node.kind == "tap" or (last and here): continue
            if stdin is None and background and not INTERACTIVE:
                stdin = os.open(os.devnull, os.O_RDONLY); pipes.append(stdin)
            if node.kind == "cmd":
                out_arg = stdout if stdout is not None else (subprocess.PIPE if (last and (capture or tee)) else None)
                err_arg = stderr if stderr is not None else (subprocess.PIPE if ((capture_err or tee) and last) else None)
                kw = {}
                if group is not None:
                    kw["process_group"] = group
                    if use_jobs: kw["preexec_fn"] = _child_setup_fg if (INTERACTIVE and not background) else _child_setup_bg if INTERACTIVE else None
                try:
                    node.proc = subprocess.Popen(node.argv, stdin=stdin, stdout=out_arg, stderr=err_arg,
                                                 env={**os.environ, **node.env} if node.env else None, **kw)
                except (FileNotFoundError, PermissionError) as e:
                    sh_error(f"{node.argv[0]}: {'command not found' if isinstance(e, FileNotFoundError) else 'permission denied'}")
                    if INTERACTIVE and not background: _tty(os.tcsetpgrp, 0, SHELL_PGID)
                    node.status = 127
            else:
                node.proc = _fork_stage(node, (stdin, stdout, stderr), pipes, group, use_jobs and INTERACTIVE and not background)
            if node.proc is not None:
                live.append(node)
                if group is not None:
                    if pgid is None: pgid = node.proc.pid
                    try: os.setpgid(node.proc.pid, pgid)
                    except OSError: pass

        # the shell keeps only the descriptors its own threads and last stage will use
        keep = set()
        for node, stdin, stdout, stderr, last in plumbing:
            if node.kind == "tap":
                copy_out = os.dup(stdout) if stdout is not None else None
                copy_in = os.dup(stdin) if stdin is not None else os.dup(0)
                taps.append((node, copy_in, copy_out, last))
            elif last and here and stdin is not None:
                keep.add(os.dup(stdin))
        final_in = next(iter(keep), None)
        final_out, final_err = plumbing[-1][2], plumbing[-1][3]
        final_out = os.dup(final_out) if here and final_out is not None else None
        final_err = os.dup(final_err) if here and final_err is not None else None
        if stream_out is not None: stream_out = os.dup(stream_out)
        for fd in pipes: os.close(fd)
        pipes = []

        if stream:
            if final.proc is not None: final.proc.stdout = os.fdopen(stream_out, "rb")
            procs = [n.proc for n in nodes]
            return Process(procs, cmdline, timeout, None, None if nodes[-1].proc else nodes[-1].status, pgid=pgid)
        killer = _Killer(timeout, [n.proc.pid for n in live], pgid) if timeout and live else None

        if use_jobs:
            if not live: return Result(final.status, None, None, time.time() - t0, cmdline)
            job = Job([n.proc.pid for n in live], pgid, cmdline)
            for n in live: n.proc.returncode = 0
            if background:
                add_job(job)
                if INTERACTIVE: print(f"[{job.id}] {job.pids[-1]}")
                return Result(0, None, None, 0, cmdline)
            rc = run_foreground(job)
            if final.proc is None: rc = final.status
            res = Result(rc, None, None, time.time() - t0, cmdline)
            if killer: killer.cancel(); res.timed_out = killer.fired
            return res

        out_buf = io.StringIO() if capture else None
        err_buf = io.StringIO() if capture_err else None
        sys.stdout.flush()
        shown_file = None
        for node, copy_in, copy_out, last in taps:
            if not last:                                  # a middle capture passes data on while we get on with the rest
                node.thread = threading.Thread(target=_tap, args=(node, copy_in, copy_out, 1 if node.show else None), daemon=True)
                node.thread.start()
                continue
            display = None                                # the last stage is a capture: copy right here
            if node.show and copy_out is None:
                if capture: shown_file = tempfile.TemporaryFile(); display = shown_file.fileno()
                else: display = 1
            _tap(node, copy_in, copy_out, display)
            if shown_file is not None:
                shown_file.seek(0); out_buf.write(shown_file.read().decode("utf-8", "replace")); shown_file.close()

        value, sink, errors, err_target = None, None, None, sys.stderr
        final.defer = bool(ns.get("quiet_failures"))
        if here and final.kind != "tap":
            sink = open(final_out, "w", errors="replace") if final_out is not None else None
            target = sink or out_buf or sys.stdout
            errors = open(final_err, "w", errors="replace") if final_err is not None and final_err != final_out else None
            try:
                err_target = errors or (sink if final_err is not None else None) or err_buf or sys.stderr
                with contextlib.redirect_stderr(err_target):
                    try: final.status, value = _run_function(final, final_in, _Out(target, keeps=capture))
                    except BrokenPipeError: final.status = 128 + signal.SIGPIPE
                target.flush()
            except BaseException:
                for f in (sink, errors):
                    if f is not None: f.close()
                raise
            final.value = value

        out = err = None
        for node in nodes:                                # wait for everything else
            if node.proc is not None:
                while True:
                    try:
                        if node is final and (capture or capture_err or tee):
                            out, err = _collect(node.proc, tee)
                        else: node.proc.wait()
                        break
                    except KeyboardInterrupt: pass
                node.status = _status(node.proc.returncode)
            if node.thread is not None: node.thread.join()
        try: _report_deferred(nodes, err_target)
        finally:
            for f in (sink, errors):
                if f is not None:
                    try: f.close()
                    except OSError: pass
        status, no_match = _judge(nodes)
        if capture and out_buf is not None and final.kind != "cmd": out = out_buf.getvalue()
        if err_buf is not None and final.kind != "cmd": err = err_buf.getvalue()
        res = Result(status, out, err, time.time() - t0, cmdline)
        res.no_match = no_match
        res.value = value
        res._statuses = [n.status for n in nodes]
        if killer: killer.cancel(); res.timed_out = killer.fired
        if final.kind == "call" and not final.name and value is not None and _at_prompt and not capture and not tee \
                and not isinstance(value, collections.abc.Iterator) and not (isinstance(value, list) and not value and final.status):
            builtins._ = value
        return res
    except KeyboardInterrupt:
        for n in nodes:
            if n.proc is not None:
                try: n.proc.wait(2)
                except (subprocess.TimeoutExpired, KeyboardInterrupt): n.proc.kill(); n.proc.wait()
        raise
    finally:
        for fd in pipes:
            try: os.close(fd)
            except OSError: pass
        for f in close_files: f.close()

def run_shell(line, capture=False, capture_err=False, tee=False, stream=False, timeout=None, merge=False):
    global last
    line = line.lstrip()
    original = line
    if line.startswith("%"): line = line[1:]              # `% cmd` forces shell mode
    forced = line != original
    line, bodies, _ = split_heredocs(line)
    if not forced: line = _python_source(line) or line
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
        except (SystemExit, KeyboardInterrupt): raise
        except Exception as e:                       # evaluating @name / @(expr) failed
            sh_error(f"{type(e).__name__}: {e}"); res = Result(1, cmd=line)
        _setlast(res)
        if _errexit and res.returncode != 0 and not res.no_match and term not in ("&&", "||", "&"): raise SystemExit(res.returncode)
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
ns["lines"] = lines
ns["quiet_failures"] = False                     # True: no Python traceback when an earlier command already failed
ns["set_pipefail"] = False                       # True: a pipeline fails if any stage does, not only the last
ns["no_match_commands"] = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "pgrep"}   # exit 1 means "found nothing"
ns["import_cwd"] = True                          # `import` finds modules in the current directory (or a script's own directory); False disables
ns["probe_flags"] = True                         # Tab on a -flag runs `cmd --help` for system programs; False disables
ns["complete_ignore_dirs"] = ("/mnt/c",)   # PATH directories hidden from command completion
ns["complete_ignore"] = (".dll", ".exe", ".vbs", ".efi", ".mof", ".bat", ".drv", ".sys", ".tlb", ".xml")   # command-name suffixes hidden from Tab completion (set in ~/.pyshrc)

KW = {"def", "class", "if", "elif", "else", "for", "while", "with", "try", "except", "finally", "import",
      "from", "async", "return", "raise", "del", "assert", "global", "nonlocal", "pass", "lambda", "yield", "match", "await"}

def _has_pipe_or_redirect(s):
    """a `|`, `<` or `>` outside quotes, brackets and comments"""
    depth, i, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            j = i + 1
            while j < n and s[j] != c: j += 2 if s[j] == "\\" else 1
            i = j
        elif c in "([{": depth += 1
        elif c in ")]}": depth -= 1
        elif c == "#": break
        elif c in "|<>" and depth <= 0: return True
        i += 1
    return False

def _splice_with_words(s):
    """`@(editor) file.txt`: words after `@(...)` can't be a decorator, so it is a command"""
    if not s.startswith("@("): return False
    try: rest = s[match_paren(s, 1):]
    except SyntaxError: return False
    return bool(rest[:1].isspace() and rest.strip() and not rest.strip().startswith(("#", ".", "(", "[")))

def _first_pipeline(s):
    """split at top-level `|` up to the first `;`, `&&`, `||` or `&` -> list of segment strings"""
    parts, depth, start, i, n = [], 0, 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            j = i + 1
            while j < n and s[j] != c: j += 2 if s[j] == "\\" else 1
            i = j
        elif c in "([{": depth += 1
        elif c in ")]}": depth -= 1
        elif c == "#": break
        elif depth <= 0 and (c == ";" or s[i:i+2] in ("&&", "||") or (c == "&" and s[i-1:i] not in ("|", ">", "<") and s[i+1:i+2] != ">")):
            break
        elif depth <= 0 and c == "|" and s[i+1:i+2] != "=":
            parts.append(s[start:i])
            if s[i+1:i+2] == "&": i += 1
            start = i + 1
        i += 1
    parts.append(s[start:i])
    return parts

# A segment starting like this is a command, never a Python operand: a path (/usr/bin/less, ./x, ../x, ~/x),
# \less, VAR=x cmd (and name=@...), a @stage, or $PAGER / $(which less) (a string, which `|` can't take).
_SURELY_COMMAND = re.compile(r"/|\.\.?/|~[\w-]*/|\\|\$(?!\(\()|@|[A-Za-z_]\w*=(?![=\s])")

def _python_source(line):
    """`p(repo) | less`: Python piped into a program.  Read it that way only when what follows a `|` is a command
    Python could not use there: a name that is not defined but is a program, or a form in _SURELY_COMMAND.
    -> the line rewritten as `@(...) | ...`, or None."""
    parts = _first_pipeline(line)
    if len(parts) < 2: return None
    g, l = _scope()
    def bound(name): return name in l or name in g or hasattr(builtins, name)
    offset = len(parts[0])
    for part in parts[1:]:
        if _SURELY_COMMAND.match(part.lstrip("&").lstrip()): break
        word = re.match(r"&?\s*([^\s;&|<>()'\"]*)", part)
        w, after = word.group(1), part[word.end():]
        if (w and re.fullmatch(r"[A-Za-z_][\w.+-]*", w) and not after.startswith("(")
                                 and not bound(w) and not keyword.iskeyword(w) and (w in aliases or w in BUILTINS or is_command(w))):
            break
        offset += 1 + len(part)
    else:
        return None
    left, rest = line[:offset].strip(), line[offset:]
    try:
        expr = preprocess(left)
        tree = ast.parse(expr, mode="eval").body
    except (SyntaxError, ValueError, RecursionError): return None
    if isinstance(tree, ast.Constant): return None                       # `'my tool' | less` names a program
    if isinstance(tree, ast.Name) and (not (tree.id in l or tree.id in g) or is_command(tree.id)): return None
    own = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
    own |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in own and not bound(n.id)
           and not n.id.startswith("__") for n in ast.walk(tree)):
        return None
    return f"@({expr}) {rest}"

def classify(line):
    return _classify(line)[0]

def _classify(line):
    """-> ("py" or "sh", why). `explain` shows the reason, so it can't drift from what actually happens."""
    s = line.strip()
    m = re.match(r"[^\s|&;<>()=]+", s)
    w = m.group() if m else ""
    if s.startswith("%"): return "sh", "a leading `%` forces shell mode"
    kw = re.match(r"[A-Za-z_]\w*", s)
    if s.startswith("@"):                                          # `@rows | sort` is a pipeline, else a decorator
        if _has_pipe_or_redirect(s): return "sh", "it starts with `@` and has a `|`, `<` or `>`: a pipeline with Python in it"
        if _splice_with_words(s): return "sh", "words follow `@(...)`, which a decorator can't have: it splices a command"
        return "py", "a line starting with `@` and no pipe or redirection is a decorator"
    if kw and kw.group() in KW and not (kw.group() in BUILTINS): return "py", f"`{kw.group()}` is a Python keyword"
    if w in BUILTINS and w not in ns and not s[len(w):].lstrip().startswith(("(", "=", ".", "[")):
        return "sh", f"`{w}` is a shell builtin (and not a Python variable)"
    if _python_source(s):
        return "sh", "what follows a `|` is a command Python couldn't use there, so the left side is Python piped into it"
    try: py = preprocess(s)
    except SyntaxError: return "sh", "it has an unbalanced `$(` or `!(`; the shell parser will report it"
    try: code = codeop.compile_command(py, "<pysh>", "single")
    except (SyntaxError, ValueError, OverflowError): return "sh", "it is not valid Python"
    if code is None:                                  # unfinished: an open bracket/string, or a block header
        if (w in ns or hasattr(builtins, w)) and s[len(w):].startswith("("): return "py", f"a call to `{w}` that continues on the next line"
        if w and w not in ns and is_command(w): return "sh", f"unfinished as Python, and `{w}` is a command"
        return "py", "an unfinished Python statement"
    try: tree = ast.parse(py)
    except SyntaxError: return "sh", "it is not valid Python"
    if tree.body and all(isinstance(x, ast.Expr) for x in tree.body) and w:
        unbound = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id not in ns and not hasattr(builtins, n.id)]
        looks_call = s[len(w):].lstrip().startswith("(")
        missing = ", ".join(f"`{n}`" for n in dict.fromkeys(unbound))
        if unbound and not looks_call and is_command(w) and w not in ns:
            return "sh", f"valid Python, but {missing} is not defined and `{w}` is a command"
        if unbound and hasattr(builtins, w) and w not in ns and not looks_call and on_path(w):
            return "sh", f"`{w}` is both a Python built-in and a program, and {missing} is not defined, so it's the program"
        if unbound:
            verb = "are" if "," in missing else "is"
            instead = "it starts like a call" if looks_call else f"`{w}` is not a command either"
            return "py", f"valid Python, and {instead}; {missing} {verb} not defined, so it would raise NameError"
        return "py", "valid Python, and every name in it is defined"
    return "py", "it is a Python statement"

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

# --------------------------------------------------------------- explain
def _render(word):
    """a parsed word, written back roughly as typed"""
    out = []
    for kind, text in word:
        out.append({"sq": f"'{text}'", "dq": '"' + text.replace("\x00", "\\") + '"', "var": f"${text}", "sub": f"$({text})",
                    "arith": f"$(({text}))", "py": f"@({text})", "call": f"@{text}"}.get(kind, text))
    return "".join(out)

def _explain_command(words, used):
    name_word = words[0]
    if any(kind in ("var", "sub", "py", "arith") for kind, _ in name_word): return "program", "chosen when the line runs"
    name = "".join(text for _, text in name_word)
    if name in BUILTINS: return "builtin", "runs inside the shell"
    if callable(aliases.get(name)): return "function alias", "a Python function"
    if "/" in name:
        full = os.path.expanduser(name)
        return "program", full if os.access(full, os.X_OK) and not os.path.isdir(full) else "not found or not executable"
    found = shutil.which(name)
    return ("program", found) if found else ("program", "NOT FOUND: this would give `command not found`")

def _explain_stage(st, i, count):
    """-> (kind, detail, text). Looks only: nothing is run, and no `@(...)` is evaluated."""
    last, ctx = i == count - 1, i > 0 or _has_input(st)
    words, env = list(st.words), []
    while words and words[0] and words[0][0][0] == "lit" and _ENV_WORD.match(words[0][0][1]) and not (ctx and _is_capture_word(words[0])):
        env.append(_render(words.pop(0)))
    text = " ".join(_render(w) for w in words) or " ".join(env)
    if not words: return "assignment", "sets environment variables", text
    w, where = words[0], "in the shell" if last else "in a forked copy of the shell (its variable changes are lost)"
    kind, value = w[0]
    if ctx and _is_capture_word(w):
        spec = value.split("=@", 1)[1] if len(w) == 1 else None
        name = value.split("=")[0]
        if spec in ("", "tee"): result = "capture", f"keeps the raw text in `{name}`" + (" and shows it" if spec else "")
        else: result = "Python, then assign", f"calls the function on the stream and puts the result in `{name}`; runs in the shell"
    elif len(w) == 1 and kind == "lit" and value == "@tee":
        result = "tee", "shows the data and passes it on"
    elif len(w) == 1 and kind == "lit" and _AT_NAME.match(value):
        try: obj, found = _dotted(value[1:]), True
        except Exception: obj, found = None, False
        source = i == 0 and not _has_input(st)
        if not found: result = "Python", f"`{value[1:]}` is NOT DEFINED: the pipeline would not start"
        elif callable(obj): result = ("Python source", f"calls it with no stream; runs {where}") if source else ("Python function", f"gets the stream; runs {where}")
        elif source: result = "Python source", "writes the value into the pipe"
        else: result = "Python", f"`{value[1:]}` is not callable: this is an error"
    elif len(w) == 1 and kind in ("py", "call"):
        if i == 0 and len(words) == 1 and not _has_input(st) and (count > 1 or _has_output(st)):
            result = "Python source", "evaluated in a forked copy; what it prints or returns goes into the pipe"
        elif kind == "call": result = "Python", f"calls it, and the function it returns gets the stream; runs {where}"
        else: result = "@( )", "a Python stage if the value is callable, otherwise spliced in as words (not evaluated here)"
    else:
        result = _explain_command(words, env)
    detail = result[1]
    if env: detail += f"; with {' '.join(env)}"
    for fd, mode, target in st.redirs:
        if mode == "dup": detail += f"; fd {fd} goes where fd {target} goes"
        elif mode == "heredoc": detail += "; reads a here-document"
        else: detail += f"; {'' if fd in (0, 1) else str(fd)}{'<<<' if mode == 'herestr' else mode} {_render(target)}"
    return result[0], detail, text

def explain_line(line):
    """What pysh would do with `line`, as text. Nothing is executed."""
    first, _, more = line.partition("\n")
    kind, why = _classify(first)
    out = [f"read as: {'Python' if kind == 'py' else 'shell command'}", f"why:     {why}"]
    if more: out.append("note:    only the first line is explained")
    if kind == "py":
        m = re.match(r"\s*([A-Za-z_][\w.+-]*)", first)
        if m and m.group(1) in ns: out.append(f"names:   `{m.group(1)}` is a Python object ({type(ns[m.group(1)]).__name__})")
        if m and shutil.which(m.group(1)): out.append(f"hint:    `{m.group(1)}` is also a program; `% {first.strip()}` runs it instead")
        return "\n".join(out)
    text = first.strip()
    forced = text.startswith("%")
    if forced: text = text[1:].strip()
    rewritten = None if forced else _python_source(text)
    if rewritten: out.append(f"as:      {rewritten}"); text = rewritten
    body, heredocs = text, []
    try:
        body = split_heredocs(text)[0]
        used = []
        items = parse(body, [("", False)] * len(heredoc_markers(body)), used)
    except (SyntaxError, ArithmeticError) as e:
        return "\n".join(out + [f"error:   syntax error: {e}"])
    for name in dict.fromkeys(used): out.append(f"alias:   {name} -> {aliases[name]}")
    joined = {"&&": "then, only if that succeeded:", "||": "then, only if that failed:", ";": "then:", "&": "(in the background) then:"}
    previous = ""
    for pipe, term in items:
        if previous: out.append(joined.get(previous, previous))
        for i, st in enumerate(pipe):
            what, detail, shown = _explain_stage(st, i, len(pipe))
            out.append(f"  {i + 1}. {what:<20} {clean(shown)}")
            out.append(f"     {clean(detail)}")
        previous = term
    if previous == "&": out.append("runs in the background")
    if any(kind in ("sub",) for pipe, _ in items for st in pipe for w in st.words for kind, _ in w) or "$(" in body:
        out.append("first:   each $( ) is run before its command, and replaced by its output")
    if any(kind == "lit" and re.search(r"[*?\[]", v) for pipe, _ in items for st in pipe for w in st.words[1:] for kind, v in w):
        out.append("globs:   unquoted * ? [ ] become the matching file names")
    if any(len(pipe) > 1 for pipe, _ in items):
        out.append(f"status:  {'any failing stage (set_pipefail is on)' if ns.get('set_pipefail') else 'that of the last stage (set_pipefail is off)'}")
    return "\n".join(out)

def b_explain(a):
    if not a: sh_error("usage: explain 'LINE'   (quote it, so that its | and > are not run)"); return 2
    print(explain_line(" ".join(a)))
    return 0

# --------------------------------------------------------------- bash_env
_BASH_OWN = {"_", "SHLVL", "PWD", "OLDPWD", "PS1", "PS2", "PS4", "PIPESTATUS", "BASH_ENV", "PYSH_BASH_ENV_OUT"}
_IDENT = re.compile(r"[A-Za-z_]\w*\Z")

def _describe_change(name, old, new):
    """`NAME=value`; for a list like PATH that already existed, just what was added and removed."""
    if old and name.endswith(("PATH", "DIRS")):
        before, after = old.split(os.pathsep), new.split(os.pathsep)
        added = [p for p in after if p not in before]
        gone = [p for p in before if p not in after]
        parts = ([f"adds {os.pathsep.join(added)}"] if added else []) + ([f"removes {os.pathsep.join(gone)}"] if gone else [])
        return f"{name}: " + ("; ".join(parts) if parts else "same entries, new order")
    return f"{name}={new}"

def b_bash_env(a):
    """bash_env [-n] [-v] FILE [ARG...] | -c 'COMMAND': run it in bash, then take over its environment changes."""
    dry = verbose = False
    command = None
    while a and a[0].startswith("-") and a[0] != "-":
        flag, a = a[0], a[1:]
        if flag == "--": break
        if flag == "-n": dry = verbose = True
        elif flag == "-v": verbose = True
        elif flag == "-c" and a: command, a = a[0], a[1:]; break
        else: command, a = None, []; break
    if command is None and not a:
        sh_error("usage: bash_env [-n] [-v] FILE [ARG...]   or   bash_env [-n] [-v] -c 'COMMANDS'"); return 2
    bash = shutil.which("bash")
    if not bash: sh_error("bash_env: bash is not installed"); return 127
    if command is None:
        path = os.path.expanduser(a[0])
        if not os.path.isfile(path): sh_error(f"bash_env: {a[0]}: no such file"); return 1
        if "/" not in path: path = "./" + path                     # `source name` would search $PATH
        script, args = 'source "$0" "$@"', [path, *a[1:]]
    else:
        script, args = command, ["bash_env", *a]
    fd, out = tempfile.mkstemp(prefix="pysh-env-"); os.close(fd)
    try:
        child = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
        child["PYSH_BASH_ENV_OUT"] = out
        sys.stdout.flush()
        # the trap also records the environment if the file calls `exit` or fails under `set -e`
        proc = subprocess.run([bash, "--noprofile", "--norc", "-c",
                               "trap 'command env -0 > \"$PYSH_BASH_ENV_OUT\"' EXIT\n" + script, *args], env=child)
        with open(out, "rb") as f: dump = f.read()
    finally:
        try: os.unlink(out)
        except OSError: pass
    if not dump:
        sh_error("bash_env: bash did not report an environment (killed, or it replaced itself); nothing changed")
        return _status(proc.returncode) or 1
    after = dict(item.split("=", 1) for item in os.fsdecode(dump).split("\0") if "=" in item)
    def ours(name): return name not in _BASH_OWN and not name.startswith(("BASH_FUNC_", "BASH_"))
    changed = {k: v for k, v in after.items() if ours(k) and os.environ.get(k) != v}
    # names bash can't hold as variables (WSL has some) are never reported as removed
    removed = [k for k in os.environ if ours(k) and _IDENT.match(k) and k not in after]
    if verbose:
        for k in sorted(changed): print(f"{'would set' if dry else 'set'} {clean(_describe_change(k, os.environ.get(k), changed[k]))}")
        for k in sorted(removed): print(f"{'would unset' if dry else 'unset'} {clean(k)}")
        if not changed and not removed: print("no environment changes")
    if not dry:
        os.environ.update(changed)
        for k in removed: os.environ.pop(k, None)
        _cmdcache.clear(); command_index.refresh()
    return _status(proc.returncode)

BUILTINS.update(explain=b_explain, bash_env=b_bash_env)

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

DEFAULT_PS1 = "{venv}{green}{user}@{host}{reset} {cyan}{cwd}{reset}{white}{branch}{reset}{red}{status}{reset}{grey}>{reset} "
_PS1_COLOURS = {name: f"\x1b[{code}m" for name, code in (("reset", 0), ("bold", 1), ("red", 31), ("green", 32), ("yellow", 33),
                ("blue", 34), ("magenta", 35), ("cyan", 36), ("white", 37), ("grey", 90))}
ns["PS1"] = DEFAULT_PS1
_ps1_reported = None

def prompt_fields():
    """What `{name}` can stand for in PS1. Names that come from outside are cleaned of control characters."""
    cwd = logical_cwd(); home = os.path.expanduser("~")
    if cwd == home or cwd.startswith(home + "/"): cwd = "~" + cwd[len(home):]
    b = git_branch()
    venv = os.environ.get("VIRTUAL_ENV")
    return dict(_PS1_COLOURS, venv=f"({clean(os.path.basename(venv.rstrip('/')))}) " if venv else "", cwd=clean(cwd),                      # the full path, never abbreviated
                user=clean(os.environ.get("USER") or "user"), host=clean(os.uname().nodename.split(".")[0]),
                branch=f"({b})" if b else "", status=f"[{last.returncode}]" if last.returncode else "")

def prompt():
    """PS1 from the namespace: a string with {fields}, or a function returning the finished prompt.
    A broken one is reported once and the default is shown instead."""
    global _ps1_reported
    ps1 = ns.get("PS1", DEFAULT_PS1)
    try:
        if callable(ps1): return str(ps1())
        return str(ps1).format_map(prompt_fields())
    except Exception as e:
        problem = f"{type(e).__name__}: {e}"
        if problem != _ps1_reported: sh_error(f"bad PS1 ({problem}); using the default"); _ps1_reported = problem
        return DEFAULT_PS1.format_map(prompt_fields())

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
    """Colour the edit buffer. A recalled multi-line entry is shown on one line (same widths as the C editor uses)."""
    return _highlight(buf).replace("\n", "\u23ce").replace("\t", " ")

def _highlight(buf):
    buf = _CTRL.sub("?", buf)                 # same width, so the cursor math still holds
    m = re.match(r"(\s*)%", buf)
    if m: return m.group(1) + C(90, "%") + highlight_sh(buf[m.end():])
    try: kind = classify(buf) if buf.strip() else "sh"
    except Exception: kind = "sh"
    out = []
    w = buf.strip()
    if kind == "py" and re.fullmatch(r"[A-Za-z_][\w.+-]*", w) and w not in ns and not hasattr(builtins, w) and not keyword.iskeyword(w):
        kind = "sh"                       # a bare word that is neither a variable nor builtin: judge it as a command
    if kind == "py": return _highlight_py(buf)
    return highlight_sh(buf)

def _highlight_py(buf):
    out = []
    for m in _PYTOK.finditer(buf):
        k, t = m.lastgroup, m.group()
        if k == "str": out.append(C(33, t))
        elif k == "cm": out.append(C(90, t))
        elif k == "n": out.append(C(36, t))
        elif k == "w" and keyword.iskeyword(t): out.append(C(34, t))
        elif k == "w" and t in ns and callable(ns[t]) or k == "w" and hasattr(builtins, t) and callable(getattr(builtins, t)): out.append(C(36, t))
        else: out.append(t)
    return "".join(out)

# --------------------------------------------------------------- @name: what is it, without running anything
_NOT_STAGES = {"sh", "shell", "aliases", "Result", "ShellError", "Process", "last", "prompt", "PS1"}    # pysh's own

def _static_lookup(name):
    """-> (state, object) for a dotted name: "callable", "value", "missing", or "unsure" when finding out would
    mean running code (a property, a module's __getattr__)."""
    first, *rest = name.split(".")
    if first in ns: obj = ns[first]
    elif hasattr(builtins, first): obj = getattr(builtins, first)
    else: return "missing", None
    for part in rest:
        try: obj_next = inspect.getattr_static(obj, part)
        except AttributeError:
            dynamic = inspect.getattr_static(obj, "__getattr__", None) is not None
            return ("unsure" if dynamic else "missing"), None
        if isinstance(obj_next, (property, types.MemberDescriptorType, types.GetSetDescriptorType)): return "unsure", None
        if isinstance(obj_next, (staticmethod, classmethod)): obj_next = obj_next.__func__
        obj = obj_next
    return ("callable" if callable(obj) else "value"), obj

def _open_paren(before):
    """is the cursor inside an unclosed `@(` or `@name(`?  -> index of its `@`, or None"""
    for m in reversed(list(re.finditer(r"@(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?\(", before))):
        depth, i, quote = 0, m.end() - 1, ""
        while i < len(before):
            c = before[i]
            if quote:
                if c == "\\": i += 1
                elif c == quote: quote = ""
            elif c in "'\"": quote = c
            elif c == "(": depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0: break
            i += 1
        if depth > 0: return None if quote else m.start()
    return None

def _name_items(prefix, source):
    """completions for the name after `@`: (name, description) pairs. `source`: nothing is piped in."""
    found = {}
    if "." in prefix:
        base, _, attr = prefix.rpartition(".")
        state, obj = _static_lookup(base)
        if obj is None: return []
        for name in dir(obj):
            if not name.startswith(attr) or (name.startswith("_") and not attr.startswith("_")): continue
            what, value = _static_lookup(f"{base}.{name}")
            if isinstance(value, types.ModuleType): found[f"{base}.{name}."] = "module"
            elif what == "callable" or (source and what == "value"): found[f"{base}.{name}"] = _pydoc(value) or type(value).__name__
        return sorted(found.items())
    for name, value in list(ns.items()):
        if name.startswith("_") or name in _NOT_STAGES or not name.startswith(prefix): continue
        if isinstance(value, types.ModuleType): found[name + "."] = "module"
        elif callable(value): found[name] = _pydoc(value) or "python function"
        elif source and not isinstance(value, (bool, set, tuple)) and name not in ("import_cwd", "complete_ignore"):
            found[name] = f"{type(value).__name__} to write into the pipe"
    if prefix:                                             # built-ins only once something is typed: there are many
        for name in dir(builtins):
            if name.startswith(prefix) and name[0].islower(): found.setdefault(name, _pydoc(getattr(builtins, name)) or "built-in")
    return sorted(found.items())

def _python_completions(before, word, head, first):
    """Tab inside Python parts of a shell line: `@name`, `name=@fn`, and inside `@(...)`. -> (head, items, nospace) or None."""
    if _open_paren(before) is not None:
        tail = re.search(r"(?:[A-Za-z_]\w*\.)*(?:[A-Za-z_]\w*)?$", before).group()
        if not tail or (before[:len(before) - len(tail)][-1:] in ("'", '"')): return None
        keep = before[:len(before) - len(tail)]
        if "." in tail:
            names = [(n.rstrip("."), d) for n, d in _name_items(tail, True)]
        else:
            pool = {k: v for k, v in ns.items() if not k.startswith("_")}
            names = [(k, _pydoc(v) or type(v).__name__) for k, v in sorted(pool.items()) if k.startswith(tail)]
            names += [(k, "built-in") for k in dir(builtins) if k.startswith(tail) and k[0].islower() and k not in pool]
        return keep, [(n, n, d) for n, d in names], True
    m = re.fullmatch(r"([A-Za-z_]\w*=)?@([\w.]*)", word)
    if not m or not (first or (m.group(1) and "|" in head)): return None
    assign, prefix = m.group(1) or "", m.group(2)
    source = not head.rstrip().endswith("|") and not assign
    items = [(f"{assign}@{n}", f"@{n}", d) for n, d in _name_items(prefix, source)]
    if not source:
        special = [("tee", "keep the raw text and show it")] if assign else [("tee", "show the data and pass it on")]
        items += [(f"{assign}@{n}", f"@{n}", d) for n, d in special if n.startswith(prefix) and not any(i[0] == f"{assign}@{n}" for i in items)]
    return head, items, False

# --------------------------------------------------------------- highlighting
_STAGE_HEAD = re.compile(r"(?<![^\s|;&])([A-Za-z_]\w*=)?@((?:[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*\.?|(?=\())?")

def _highlight_python_part(buf, i, command_position, piped):
    """highlight an `@...` token starting at buf[i]. -> (coloured text, end index) or None if it isn't one"""
    m = _STAGE_HEAD.match(buf, i)
    if not m: return None
    assign, name, end = m.group(1) or "", m.group(2), m.end()
    if assign and not piped: return None                                 # `X=@y cmd` only sets an environment variable
    opens = buf[end:end + 1] == "("
    if not command_position and not opens: return None                   # `echo @x` is just text
    if not command_position and name: return None                        # `echo @f(x)` too
    out = assign
    if name is None and not opens:                                       # a bare `@`, as in `name=@`
        return (out + C(36, "@"), end) if assign else None
    if name:
        state = "callable" if name == "tee" else _static_lookup(name.rstrip("."))[0]
        usable = state in ("callable", "unsure") or (state == "value" and not piped and not assign and not opens)
        out += C(36 if usable else 31, "@" + name)
    else: out += C(36, "@")
    if opens:
        depth, j, quote = 0, end, ""
        while j < len(buf):
            c = buf[j]
            if quote:
                if c == "\\": j += 1
                elif c == quote: quote = ""
            elif c in "'\"": quote = c
            elif c == "(": depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0: break
            j += 1
        closed = j < len(buf) and depth == 0
        out += C(36, "(") + _highlight_py(buf[end + 1:j]) + (C(36, ")") if closed else "")
        end = j + 1 if closed else len(buf)
    return out, end

def highlight_sh(buf):
    out = []
    cmd, piped, pos = True, False, 0
    while pos < len(buf):
        if buf[pos] == "@" or (cmd and re.match(r"[A-Za-z_]\w*=@", buf[pos:])):
            if pos == 0 or buf[pos - 1] in " \t|;&":
                redirected_in = re.search(r"(?<![<>0-9])<", buf[pos:]) is not None
                part = _highlight_python_part(buf, pos, cmd, piped or redirected_in)
                if part is not None:
                    out.append(part[0]); pos = part[1]; cmd = False; continue
        m = _SHTOK.match(buf, pos)
        pos = m.end()
        k, t = m.lastgroup, m.group()
        if k == "w":
            if cmd and re.match(r"[A-Za-z_]\w*=", t): out.append(t); continue
            if cmd:
                out.append(t); cmd = False                          # the program name keeps the default colour, like its arguments
            elif t.startswith("$"): out.append(C(36, t))
            elif os.path.exists(os.path.expanduser(t)) and not t.startswith("-"): out.append(f"\x1b[4m{t}\x1b[0m")
            else: out.append(t)
        elif k == "str": out.append(C(33, t)); cmd = False if not cmd else cmd
        elif k == "op":
            out.append(C(36, t))
            if t in ("|", "||", "&&", ";", "&"): cmd, piped = True, t == "|"
        elif k == "cm": out.append(C(90, t))
        else: out.append(t)
    return "".join(out)

_helpcache = {}
def flags_for(cmd):
    """Parse `cmd --help` into [(flag, description)] (cached, 1.5s timeout).

    This runs the program, so it is limited to system programs (see trusted_command) and can be turned off
    with `probe_flags = False` in ~/.pyshrc."""
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
               "activate": "use a virtualenv", "deactivate": "leave the virtualenv",
               "explain": "show how a line would be read", "bash_env": "take environment changes from a bash script",
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
    python = _python_completions(before, word, head, first) if "@" in before else None
    if python is not None:
        head, items, nospace = python
        if not items: return before, []
        out = [(head + w + ("" if nospace or w.endswith(".") else " "), disp, desc) for w, disp, desc in items]
        return head + os.path.commonprefix([w for w, _, _ in items]), out
    custom = None if first or word.startswith("$") else _custom_completions(word, stage)
    if custom is not None:
        items = custom
    elif word.startswith("$") and not word.startswith("$("):
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
                listed = command_index.names(d)
                for f in (os.listdir(d) if listed is None else listed):
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

def _custom_completions(word, stage):
    """items from a registered completer, or None to complete as usual"""
    fn = completers.get(stage[0])
    if fn is None: return None
    try:
        offered = fn(word, stage[1:])
        if offered is None: return None
        items = []
        for entry in offered:
            text, desc = entry if isinstance(entry, tuple) else (entry, "")
            text = str(text)
            if text.startswith(word) and not _CTRL.search(text) and "\n" not in text and "\t" not in text:
                items.append((_esc(text), text, clean(str(desc))[:70]))
        return items
    except Exception: return None                          # a broken completer must not break the editor

def search_history(query):
    """Ctrl-R: newest-first, de-duplicated, case-insensitive substring matches."""
    q, seen, out = query.lower(), set(), []
    for h in reversed(history):
        if h in seen or q not in h.lower(): continue
        seen.add(h)
        out.append((h, h.replace("\n", " ⏎ "), ""))
        if len(out) >= 200: break
    return out

# One entry per line in the file. An entry with several lines is written with a leading \x1e and
# its newlines escaped; single-line entries are stored as they are.
_HIST_MULTI = "\x1e"
_HIST_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r"}

def _history_encode(entry):
    if not any(c in entry for c in "\n\r") and not entry.startswith(_HIST_MULTI): return entry
    return _HIST_MULTI + "".join(_HIST_ESCAPES.get(c, c) for c in entry)

def _history_decode(line):
    if not line.startswith(_HIST_MULTI): return line
    return re.sub(r"\\([\\nr])", lambda m: {"n": "\n", "r": "\r"}.get(m.group(1), "\\"), line[1:])

_RECORD_LIMIT = 20000

def history_record():
    """where each command's time, directory and exit status are kept (for pyshlib's `hist`)"""
    return HIST + ".jsonl"

def record_history(line, cwd, started, status):
    if line.startswith(" ") or not line.strip(): return          # same rule as the history file
    try:
        fd = os.open(history_record(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"cmd": str(line), "time": round(started, 2), "cwd": cwd, "status": status}) + "\n")
    except (OSError, ValueError): pass

def trim_record():
    """keep the record from growing for ever: the newest entries, once it passes a few megabytes"""
    path = history_record()
    try:
        if os.path.getsize(path) <= 4_000_000: return
        with open(path, errors="replace") as f: keep = f.readlines()[-_RECORD_LIMIT:]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".pysh-record-")
        with os.fdopen(fd, "w") as f: f.writelines(keep)
        os.replace(tmp, path)
    except OSError: pass

def load_history():
    try:
        st = os.stat(HIST)
        if st.st_uid == os.getuid() and st.st_mode & 0o077: os.chmod(HIST, 0o600)       # it may hold secrets
        with open(HIST) as f: history.extend(_history_decode(l.rstrip("\n")) for l in f if l.strip())
    except OSError: pass

def save_history(line):
    if line.startswith(" "): return                 # like bash's ignorespace: a leading space keeps a command out of history
    if line.strip() and (not history or history[-1] != line):
        history.append(line)
        try:
            fd = os.open(HIST, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f: f.write(_history_encode(line) + "\n")
        except OSError: pass

class Block(str):
    """An entry that came back from the editor, or several lines recalled from history: run as a script."""

class _EditRequest(Exception):
    def __init__(self, text): self.text = text

def run_block(src):
    if "\n" not in src: return run_line(src)
    if classify(src.split("\n", 1)[0]) == "py":
        try: compile(preprocess(src) + "\n", "<pysh>", "single")       # one Python statement: echo values, as typed
        except (SyntaxError, ValueError): pass
        else: return run_python(src)
    return run_script(src)

def edit_in_editor(text):
    """Alt-E: edit `text` in $EDITOR (default vim). -> the Block to run, or "" to run nothing.
    It runs only if the file was saved, so quitting without writing (`:q!`) is a way out."""
    try: argv = shlex.split(os.environ.get("EDITOR") or "vim") or ["vim"]
    except ValueError as e: sh_error(f"$EDITOR: {e}"); return ""
    argv[0] = os.path.expanduser(argv[0])
    fd, path = tempfile.mkstemp(prefix="pysh-edit-", suffix=".pysh")
    keep, result = False, ""
    try:
        with os.fdopen(fd, "w") as f: f.write(text + "\n" if text else "")
        os.utime(path, ns=(0, 0))                                  # any write now shows, however quick
        stage = Stage()
        stage.words = [[("sq", a)] for a in argv + [path]]          # quoted: no aliases, globs or expansion
        r = run_pipeline([stage], cmdline=" ".join(argv))
        if r.returncode == 148:
            keep = True; sh_error(f"editor stopped; nothing run (your text is in {path})")
        elif r.returncode != 0:
            sh_error(f"editor exited with status {r.returncode}; nothing run")
        elif os.stat(path).st_mtime_ns != 0:
            with open(path, errors="replace") as f: new = f.read().replace("\r\n", "\n")
            new = re.sub(r"\A(?:[ \t]*\n)+", "", new).rstrip()
            if new.strip(): result = Block(new)
    except OSError as e:
        sh_error(f"edit: {e}")
    finally:
        if not keep:
            try: os.unlink(path)
            except OSError: pass
    if result:
        for line in result.split("\n"): print("\x1b[90m\u2026\x1b[0m " + clean(line.replace("\t", "    ")))
    elif text.strip():
        save_history(text)                                         # don't lose what had been typed: Up brings it back
    return result

def read_entry(tty):
    """Read one logical entry (with continuation handling)."""
    import _pyshline
    lines = []
    def rl(p):
        if tty:
            global _typing
            _typing = True
            try: got = _pyshline.readline(p, history, complete, suggest, highlight, search_history)
            finally: _typing = False
            if isinstance(got, tuple): raise _EditRequest(got[0])
            return got
        sys.stdout.flush()
        l = sys.stdin.readline()
        if not l: raise EOFError
        return l.rstrip("\n")
    try:
        if INTERACTIVE: reap_jobs(notify=True)
        _cmdcache.clear()
        command_index.refresh()
        older = ns.get("prompt")                         # a `prompt` string or function still wins over PS1
        text = prompt() if older is None else str(older() if callable(older) else older)
        above, newline, text = text.rpartition("\n")    # the line editor draws one line; print any earlier ones first
        if newline: print(above, flush=True)
        first = rl(text)
        if "\n" in first: return Block(first)                       # several lines recalled from history
        if not first.strip(): return first
        lines.append(first)
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
    except _EditRequest as e:
        return edit_in_editor("\n".join(lines + [e.text]))

DEFAULT_RC = """\
# ~/.pyshrc - run by every interactive pysh. pysh created this file because it didn't exist; edit it freely.
# It takes the same syntax as a script, and whatever it defines at the top level stays in your session:
# imports, variables, functions, aliases. Environment variables need `export`.

# Colour `ls` output, if this ls supports it. (which_safe never picks up a ./ls from the current directory.)
_ls = shell.which_safe("ls")
if _ls and !(@(_ls) --color=auto -d /):
    aliases["ls"] = "ls --color=auto"
del _ls

# Settings, shown with their defaults. Uncomment to change.
# probe_flags = True                  # Tab on a -flag runs `cmd --help` (system programs only)
# import_cwd = True                   # `import` finds modules in the current directory
# complete_ignore = (".dll", ".exe", ".vbs", ".efi", ".mof", ".bat", ".drv", ".sys", ".tlb", ".xml")
# complete_ignore_dirs = ("/mnt/c",)  # PATH directories left out of command completion
# set_pipefail = False                # True: a pipeline fails if any of its stages does
# quiet_failures = False              # True: no Python traceback when an earlier command already failed
# no_match_commands |= {"mytool"}     # more commands whose exit status 1 means "found nothing" (for | x=@...)

# The prompt. Fields: {venv} {user} {host} {cwd} {branch} {status}
# Colours: {red} {green} {yellow} {blue} {magenta} {cyan} {white} {grey} {bold} {reset}
PS1 = "@PS1@"

# Examples
# export EDITOR=nano                       # Alt-E opens $EDITOR (default vim)
# bash_env /etc/profile                    # take over what a bash script sets (nvm, cargo, conda, ...)
# from pyshlib import table, where, freq   # extras; or: from pyshlib import *   (see `man pysh`)
# export PATH=~/bin:$PATH
# alias ll='ls -l'
# def PS1(): return $(date +%H:%M) + " > "        # a function can compute anything
""".replace("@PS1@", DEFAULT_PS1)

def create_default_rc(path):
    """Write the starter ~/.pyshrc, never over anything that exists. Only into a home directory that is ours,
    so `sudo pysh` with $HOME kept doesn't leave a root-owned file there. -> True if the file was written."""
    home = os.path.dirname(path)
    try:
        if os.path.lexists(path) or os.stat(home).st_uid != os.geteuid(): return False
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w") as f: f.write(DEFAULT_RC)
        return True
    except OSError:
        return False

def run_rc(create=True):
    """Interactive startup: run ~/.pyshrc like a script (shell and Python lines) in the shell's namespace,
    creating it first if there is none. If it can't be created, the starter text still runs, from memory.
    An error is reported and does not stop the shell."""
    rcf = os.path.expanduser("~/.pyshrc")
    if create: create_default_rc(rcf)
    here = "" in sys.path
    while "" in sys.path: sys.path.remove("")          # the startup file must not import from whatever directory we opened in
    try:
        if os.path.isfile(rcf):
            with open(rcf) as f: text = f.read()
            ns["__file__"] = rcf                          # as in a script, while it runs
            run_script(text, rcf)
        elif not os.path.lexists(rcf):
            run_script(DEFAULT_RC)
    except KeyboardInterrupt: print("\n.pyshrc: interrupted", file=sys.stderr)
    except SystemExit: raise
    except Exception: traceback.print_exc()
    finally:
        ns.pop("__file__", None)
        if here and "" not in sys.path: sys.path.insert(0, "")
        _setlast(Result())                             # a failing line in the rc file shouldn't mark the first prompt

def repl(interactive=False, norc=False):
    global _at_prompt
    tty = os.isatty(0) and os.isatty(1)
    global _exit_warned
    if tty:
        init_job_control()
        load_history()
        trim_record()
        os.environ.setdefault("EDITOR", "vim")                      # Alt-E, git and friends; ~/.pyshrc can change it
    if (tty or interactive) and not norc:
        try: run_rc(create=tty)
        except SystemExit as e: hup_jobs(); return exit_status(e)        # `exit` in an rc file leaves, as in bash
    drop_import_cwd()
    if tty: command_index.start()                                   # after the rc file, which may change $PATH
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
        _at_prompt = True
        where_run, started = logical_cwd() if tty else "", time.time()
        try: run_block(line) if isinstance(line, Block) else run_line(line)
        except SystemExit as e: hup_jobs(); return exit_status(e)
        except KeyboardInterrupt: print(); _setlast(Result(130, cmd=line))
        except Exception as e:
            sh_error(f"internal error: {type(e).__name__}: {e}"); _setlast(Result(70, cmd=line))
        if tty: record_history(line, where_run, started, last.returncode)
    if tty: print()
    hup_jobs()
    return last.returncode

def setup_import_path(first):
    """Like python: the REPL, -c and piped input search the current directory ('' follows `cd`); a script file
    searches its own directory. PYSH_IMPORT_CWD=0 turns it off here; `import_cwd = False` in ~/.pyshrc does too."""
    if os.environ.get("PYSH_IMPORT_CWD", "1") in ("0", "false", "no", ""): return
    if first not in sys.path: sys.path.insert(0, first)

def drop_import_cwd():
    """After ~/.pyshrc: honour `import_cwd = False` by removing the current directory from the search path."""
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

def parse_options(args):
    """-> (set of option letters/names, the remaining arguments). Raises ValueError for an unknown option.
    Short options combine (`-lic`), as other programs expect of a shell."""
    opts, i = set(), 0
    longs = {"--version": "V", "--help": "h", "--login": "l", "--norc": "norc", "--interactive": "i"}
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]; i += 1
        if a == "--": break
        if a.startswith("--"):
            if a not in longs: raise ValueError(a)
            opts.add(longs[a])
        else:
            for ch in a[1:]:
                if ch not in "Vhceil": raise ValueError("-" + ch)
                opts.add(ch)
    return opts, args[i:]

def run_startup(norc):
    """-i with -c or a script: read ~/.pyshrc first (never creating it). -> an exit status if it called `exit`."""
    if norc: return None
    try: run_rc(create=False)
    except SystemExit as e: return exit_status(e)
    return None

def main(argv):
    global _batch, _errexit
    fix_pwd()
    try: opts, args = parse_options(list(argv[1:]))
    except ValueError as e: sh_error(f"unknown option {e}"); print(USAGE, file=sys.stderr); return 2
    if "V" in opts:
        print(f"pysh {VERSION}\nPython {sys.version}\nhome {sys.prefix}"); return 0
    if "h" in opts: print(HELP.replace("{builtins}", " ".join(sorted(BUILTINS)))); return 0
    if "e" in opts: _errexit = True
    # -l / --login / argv[0] "-pysh" are accepted so that login(1), ssh, tmux and editors can start pysh;
    # there is no separate login file to read.
    interactive, norc = "i" in opts, "norc" in opts
    adopt_venv()
    if "c" in opts:
        if not args: sh_error("-c needs an argument"); return 2
        sys.argv = ["pysh"] + args[1:]
        setup_import_path("")
        if interactive and (status := run_startup(norc)) is not None: return status
        _batch = True
        try: run_script(args[0])
        except SystemExit as e: return exit_status(e)
        return last.returncode
    if args:
        path = args[0]
        sys.argv = args
        setup_import_path(os.path.dirname(os.path.realpath(path)))
        try:
            with open(path) as f: text = f.read()
        except OSError as e: sh_error(f"{path}: {e.strerror}"); return 127
        if interactive and (status := run_startup(norc)) is not None: return status
        sys.argv, _batch = args, True
        ns["__file__"] = os.path.abspath(path)
        try: run_script(text, path)
        except SystemExit as e: return exit_status(e)
        return last.returncode
    setup_import_path("")
    return repl(interactive, norc)

if __name__ == "__main__":
    sys.stdout.flush()
    raise SystemExit(main(sys.argv))

"""pyshlib - extras for pysh. Built into the shell and loaded only when imported:

    from pyshlib import table, where, freq        # in ~/.pyshrc, or a script

Every name you import counts as Python from then on, so import what you use.
"""
import collections, concurrent.futures, contextlib, datetime as _dt, functools, itertools, json, math, os, re, shlex, subprocess, sys

__all__ = ["table", "fields", "where", "each", "freq", "stats", "jsonl", "spark", "peek",
           "par", "hist", "in_dir", "in_env", "on_cd", "completer"]

# `_shell` (pysh's own internals) is put here by the loader.

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def _clean(value):
    """for display: control characters (terminal escapes, line breaks) become '?'"""
    return _CTRL.sub("?", str(value))

def _lines(source):
    """lines without newlines, from a stream, a string or any iterable"""
    if isinstance(source, str): source = source.splitlines()
    return (str(line).rstrip("\n") for line in source)

def _stage(function):
    """`@freq`, `@freq 10` and `@freq(10)` all work: called with a number (or nothing) first, return the stage."""
    @functools.wraps(function)
    def wrapper(source=None, *args, **kwargs):
        if source is None or isinstance(source, int):
            first = () if source is None else (source,)
            return lambda stream, *more: function(stream, *first, *args, *more, **kwargs)
        return function(source, *args, **kwargs)
    return wrapper

# ------------------------------------------------------------ pipeline stages
class Table:
    """Rows and named columns. Prints aligned; `t[0]` is a row dict, `t["name"]` a column, `t.df()` a DataFrame."""
    def __init__(self, columns, rows): self.columns, self.rows = list(columns), [list(r) for r in rows]
    def __len__(self): return len(self.rows)
    def __iter__(self): return (dict(zip(self.columns, r)) for r in self.rows)
    def __getitem__(self, key):
        if isinstance(key, str):
            if key not in self.columns: raise KeyError(key)
            i = self.columns.index(key)
            return [r[i] for r in self.rows]
        if isinstance(key, slice): return Table(self.columns, self.rows[key])
        return dict(zip(self.columns, self.rows[key]))
    def df(self):
        """as a pandas DataFrame, with numeric columns converted"""
        import pandas as pd
        frame = pd.DataFrame(self.rows, columns=self.columns)
        for name in frame.columns:
            try: frame[name] = pd.to_numeric(frame[name])
            except (ValueError, TypeError): pass
        return frame
    def __str__(self):
        grid = [[_clean(c) for c in row] for row in [self.columns, *self.rows]]
        if not self.columns: return ""
        width = [max(len(row[i]) for row in grid) for i in range(len(self.columns))]
        number = re.compile(r"-?[\d,]*\.?\d+[%KMGT]?")
        right = [bool(self.rows) and all(number.fullmatch(row[i]) for row in grid[1:] if row[i]) for i in range(len(width))]
        return "\n".join("  ".join(c.rjust(w) if r else c.ljust(w) for c, w, r in zip(row, width, right)).rstrip() for row in grid)
    __repr__ = __str__

def table(source, *options):
    """`ps aux | @table`: whitespace (or `@table ,` for CSV) columns, first line as header.
    `noheader` numbers the columns instead. The last column takes the rest of the line."""
    sep = next((o for o in options if o != "noheader"), None)
    text = [line for line in _lines(source) if line.strip()]
    if not text: return Table([], [])
    if sep is not None:
        import csv
        parsed = [row for row in csv.reader(text, delimiter="\t" if sep in ("\\t", "tab") else sep)]
        n = max(map(len, parsed))
        parsed = [row + [""] * (n - len(row)) for row in parsed]
    else:
        # as many columns as the header has names, unless the rows are narrower (df's "Mounted on" is one column)
        n = min(len(line.split()) for line in text)
        parsed = [line.split(None, n - 1) for line in text]
        parsed = [row + [""] * (n - len(row)) for row in parsed]
    if "noheader" in options: return Table([str(i + 1) for i in range(len(parsed[0]))], parsed)
    return Table(parsed[0], parsed[1:])

@_stage
def fields(source, *numbers):
    """`df -P | @fields 6 5`: those whitespace-separated fields, in that order, joined by tabs.
    Counting starts at 1; negative numbers count from the end."""
    wanted = [int(n) for n in numbers]
    if not wanted or 0 in wanted: raise ValueError("fields: give field numbers, starting at 1")
    def generate():
        for line in _lines(source):
            parts = line.split()
            yield "\t".join(parts[n - 1 if n > 0 else n] if -len(parts) <= n <= len(parts) else "" for n in wanted)
    return generate()

def _function_first(name, function):
    if hasattr(function, "read") or not callable(function):
        raise TypeError(f"write @{name}(function), for example @{name}(str.upper)")

def where(function, *extra):
    """`ls | @where(lambda l: l.endswith(".py"))`: the lines for which the function is true."""
    _function_first("where", None if extra else function)
    return lambda source: (line for line in _lines(source) if function(line))

def each(function, *extra):
    """`ls | @each(os.path.getsize)`: the function's result for every line (None is left out)."""
    _function_first("each", None if extra else function)
    return lambda source: (value for value in map(function, _lines(source)) if value is not None)

class Freq:
    """(item, count) pairs, commonest first. `f["x"]` is a count; prints with bars."""
    def __init__(self, pairs): self.pairs = pairs
    def __len__(self): return len(self.pairs)
    def __iter__(self): return iter(self.pairs)
    def __getitem__(self, key):
        if isinstance(key, int): return self.pairs[key]
        return dict(self.pairs).get(key, 0)
    def __str__(self):
        if not self.pairs: return ""
        top = max(n for _, n in self.pairs); width = len(str(top))
        return "\n".join(f"{n:>{width}}  {'█' * max(1, round(n / top * 20)):<20}  {_clean(item)}" for item, n in self.pairs)
    __repr__ = __str__

@_stage
def freq(source, top=None):
    """`... | @freq` or `@freq 10`: how often each line occurs."""
    return Freq(collections.Counter(_lines(source)).most_common(None if top is None else int(top)))

def _numbers(source, column):
    """-> (finite numbers, how many lines had none)"""
    found, skipped = [], 0
    for line in _lines(source):
        if not line.strip(): continue
        try:
            text = line.split()[int(column) - 1] if column is not None else line.strip()
            value = float(text.rstrip("%"))
            if not math.isfinite(value): raise ValueError
        except (ValueError, IndexError): skipped += 1; continue
        found.append(value)
    return found, skipped

def _quantile(ordered, q):
    at = (len(ordered) - 1) * q; low = int(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)

class Stats:
    """count, sum, min, mean, median, p95, max, stdev and skipped (lines without a number)."""
    def __init__(self, values, skipped):
        ordered = sorted(values)
        self.count, self.skipped = len(ordered), skipped
        self.sum = math.fsum(ordered)
        some = bool(ordered)
        self.min = ordered[0] if some else None
        self.max = ordered[-1] if some else None
        self.mean = self.sum / len(ordered) if some else None
        self.median = _quantile(ordered, 0.5) if some else None
        self.p95 = _quantile(ordered, 0.95) if some else None
        self.stdev = math.sqrt(math.fsum((v - self.mean) ** 2 for v in ordered) / (len(ordered) - 1)) if len(ordered) > 1 else None
    def __str__(self):
        if not self.count: return "no numbers" + (f" ({self.skipped} lines skipped)" if self.skipped else "")
        show = lambda v: "-" if v is None else f"{v:,.0f}" if float(v).is_integer() else f"{v:,.4g}"
        out = "  ".join(f"{k} {show(getattr(self, k))}" for k in ("count", "min", "mean", "median", "p95", "max", "sum", "stdev"))
        return out + (f"  ({self.skipped} lines skipped)" if self.skipped else "")
    __repr__ = __str__

@_stage
def stats(source, column=None):
    """`... | @stats` or `@stats 2` for the second field: summary of the numbers."""
    return Stats(*_numbers(source, column))

def jsonl(source):
    """`tail -f app.jsonl | @jsonl`: one object per JSON line, as they arrive. Bad lines are skipped, noted once."""
    warned = False
    for number, line in enumerate(_lines(source), 1):
        if not line.strip(): continue
        try: value = json.loads(line)
        except ValueError:
            if not warned: print(f"jsonl: line {number} is not JSON; skipping such lines", file=sys.stderr); warned = True
            continue
        yield value

class Spark:
    """a one-line chart"""
    def __init__(self, values):
        self.values = values
        if values:
            low, span = min(values), (max(values) - min(values))
            self.text = "".join("▁▂▃▄▅▆▇█"[int((v - low) / span * 7 + 0.5) if span else 0] for v in values)
        else: self.text = ""
    def __str__(self): return self.text
    __repr__ = __str__

@_stage
def spark(source, column=None):
    """`... | @spark` or `@spark 2`: the numbers as a one-line chart. Also `spark([3, 1, 4])`."""
    if not isinstance(source, str) and not hasattr(source, "read"):
        source = list(source)
        if all(isinstance(v, (int, float)) for v in source): return Spark([float(v) for v in source])
    return Spark(_numbers(source, column)[0])

@_stage
def peek(source, count=5):
    """`... | @peek | ...`: show the first lines on the terminal (standard error) and pass everything on."""
    def generate(n=int(count)):
        for i, line in enumerate(source):
            if i < n: print(_clean(str(line).rstrip("\n")), file=sys.stderr, flush=True)
            yield line
    return generate()

# ------------------------------------------------------------ running things
_PAR_SPLICE = "@(_par_item)"

def _template(command):
    """`ssh {} uptime` -> a command that splices the item as one argument. A quoted {} is refused."""
    if "{}" not in command: raise ValueError("par: the command needs {} where each item goes")
    text = command.replace("{}", _PAR_SPLICE)
    for token in _shell.tokenize(text):
        if token[0] != "w": continue
        if any(kind != "py" and _PAR_SPLICE in str(value) for kind, value in token[1]):
            raise ValueError("par: write {} without quotes (each item is already passed as one argument)")
    return text

class _Results(list):
    """what par() with a command returns: a list of Results, which also knows how to end a pipeline stage"""
    def _as_stage(self, out):
        """write the output, pass error messages on, and -> the stage's status: 1 if any item failed"""
        for r in self:
            if r.out:
                out.write(r.out if r.out.endswith("\n") else r.out + "\n")
            out.flush()                                    # keep each item's output ahead of its errors
            if r.err: sys.stderr.write(r.err if r.err.endswith("\n") else r.err + "\n")
            if r.returncode and (r.timed_out or not r.err):          # nothing else would say what went wrong
                why = "timed out" if r.timed_out else f"exit status {r.returncode}"
                print(f"par: {_clean(r.item)}: {why}", file=sys.stderr)
        return 1 if any(r.returncode for r in self) else 0

def par(items, command, jobs=8, timeout=None):
    """Run a command (or a function) for every item, `jobs` at a time. -> the results in the items' order.

        for r in par(hosts, "ssh {} uptime"): print(r.item, r.returncode, r.out)
        par(files, os.path.getsize)
        cat hosts.txt | @par 'ssh {} uptime'          (the items are the lines, without their newlines)

    As a stage it prints each command's output and errors, and fails if any item did (after trying them all).

    In a command, {} is the item as ONE argument, so spaces or `;` in it do nothing. Don't quote it. An item
    starting with `-` is still read as an option by many programs: put `--` before {} if that matters."""
    items = list(_lines(items) if hasattr(items, "read") else items)       # a stream: a pipeline stage, or a file
    if int(jobs) < 1: raise ValueError("par: jobs must be at least 1")
    if callable(command): run = command
    else:
        text = _template(command)
        shell_sh = _shell.ns["sh"]                    # looks up @(...) in the caller's frame: `run` below
        def run(item):
            _par_item = str(item)
            try: result = shell_sh(text, timeout=timeout)
            except subprocess.TimeoutExpired:
                result = _shell.Result(124, "", "", timeout, command); result.timed_out = True
            result.item = item
            return result
    errexit, _shell._errexit = _shell._errexit, False             # a failing item is a result, not the end of the script
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=int(jobs))
    try:
        results = pool.map(run, items)
        return list(results) if callable(command) else _Results(results)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        _shell._errexit = errexit

# ------------------------------------------------------------ history
class Entry:
    """one command from the history record: .cmd .time .cwd .status"""
    def __init__(self, cmd, time, cwd, status): self.cmd, self.time, self.cwd, self.status = cmd, time, cwd, status
    def __str__(self):
        home = os.path.expanduser("~")
        cwd = "~" + self.cwd[len(home):] if self.cwd == home or self.cwd.startswith(home + "/") else self.cwd
        mark = f"[{self.status}] " if self.status else ""
        return f"{self.time:%Y-%m-%d %H:%M}  {mark}{_clean(cwd)}  {_clean(self.cmd.replace(chr(10), ' ⏎ '))}"
    __repr__ = __str__

class Entries(list):
    """a list of Entry that prints one per line"""
    def __repr__(self): return "\n".join(map(str, self)) if self else "(nothing found)"
    __str__ = __repr__

class _History:
    """What was run, when, where and how it ended (interactive shells record it). Oldest first."""
    def _all(self):
        found = Entries()
        try:
            with open(_shell.history_record(), errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        found.append(Entry(str(d["cmd"]), _dt.datetime.fromtimestamp(d["time"]), str(d["cwd"]), int(d["status"])))
                    except (ValueError, KeyError, TypeError, OverflowError, OSError): continue
        except OSError: pass
        return found

    def search(self, text=None, cwd=None, failed=None, since=None, limit=None):
        """hist.search("docker", cwd=".", failed=True, since=3600)
        text: substring, ignoring case.  cwd: run in that directory.  failed: True/False by exit status.
        since: seconds ago, a timedelta or a datetime.  limit: only the newest N."""
        if isinstance(since, (int, float)): since = _dt.datetime.now() - _dt.timedelta(seconds=since)
        elif isinstance(since, _dt.timedelta): since = _dt.datetime.now() - since
        where_ = None if cwd is None else os.path.realpath(os.path.expanduser(cwd))
        out = Entries(e for e in self._all()
                      if (text is None or text.lower() in e.cmd.lower())
                      and (where_ is None or os.path.realpath(e.cwd) == where_)
                      and (failed is None or bool(e.status) == bool(failed))
                      and (since is None or e.time >= since))
        return Entries(out[-limit:]) if limit else out

    def last(self, n=10): return self.search(limit=n)
    def __repr__(self): return repr(self.last(20))

hist = _History()

# ------------------------------------------------------------ temporary changes
@contextlib.contextmanager
def in_dir(path):
    """`with in_dir("/tmp"):` - work there, then come back. (Changes the whole process: not for threads.)"""
    before, pwd = os.getcwd(), os.environ.get("PWD")
    os.chdir(os.path.expanduser(path))
    os.environ["PWD"] = os.getcwd()
    try: yield os.getcwd()
    finally:
        os.chdir(before)
        if pwd is None: os.environ.pop("PWD", None)
        else: os.environ["PWD"] = pwd

@contextlib.contextmanager
def in_env(**variables):
    """`with in_env(CC="clang", DEBUG=None):` - set (None removes) environment variables, then put them back."""
    before = {name: os.environ.get(name) for name in variables}
    def apply(values):
        for name, value in values.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = str(value)
    try:
        apply(variables); yield
    finally:
        apply(before)

# ------------------------------------------------------------ hooks
def on_cd(function):
    """Run `function(new_directory)` after every `cd`.

        @on_cd
        def auto_venv(path):
            if os.path.isdir(".venv"): $[activate .venv]
    """
    key = (function.__module__, function.__qualname__)
    # sourcing ~/.pyshrc again replaces the hook instead of adding a second one
    _shell.cd_hooks[:] = [h for h in _shell.cd_hooks if (h.__module__, h.__qualname__) != key]
    _shell.cd_hooks.append(function)
    return function

def completer(command):
    """Your own Tab completion for a command's arguments.

        @completer("deploy")
        def deploy_targets(word, args):          # the word being completed, the arguments before it
            return ["staging", ("production", "careful!")]

    Return strings or (string, description) pairs; those starting with `word` are offered. None falls back to
    the usual completion."""
    def register(function):
        _shell.completers[command] = function
        return function
    return register

#!/usr/local/bin/pysh
"""diskhog - where did the disk space go?

Runs `du` on a directory, shows the biggest children as a table with bars, and (using a small
sqlite database) how much each one has grown or shrunk since the last time you ran it.
"""
import argparse, os, re, sqlite3, sys, time
import pandas as pd
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

def human(kb):
    n = float(kb) * 1024
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024

def signed(kb):
    return "=" if kb == 0 else ("+" if kb > 0 else "-") + human(abs(kb))

def scan(path, depth):
    """One `du` call; returns (DataFrame of children, total_kb, number of unreadable entries)."""
    r = !(du -x -k -d @(str(depth)) @(path))          # -x: stay on one filesystem
    rows = []
    for line in r.out.splitlines():
        size, _, name = line.partition("\t")
        if size.isdigit(): rows.append((name, int(size)))
    if not rows:
        sys.exit(f"diskhog: could not read {path}: {r.err.strip() or 'no output from du'}")
    df = pd.DataFrame(rows, columns=["path", "kb"])
    total = int(df.loc[df.path == path, "kb"].iloc[0]) if (df.path == path).any() else int(df.kb.max())
    df = df[df.path != path]
    return df, total, len(r.err.splitlines())

def open_db(file):
    os.makedirs(os.path.dirname(file), exist_ok=True)
    db = sqlite3.connect(file)
    db.execute("create table if not exists runs (ts real, root text, path text, kb integer)")
    return db

def previous(db, root):
    """sizes from the most recent earlier run on this root: {path: kb}, timestamp"""
    row = db.execute("select max(ts) from runs where root = ?", (root,)).fetchone()
    if not row or row[0] is None: return {}, None
    return dict(db.execute("select path, kb from runs where root = ? and ts = ?", (root, row[0]))), row[0]

def main(argv=None):
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    ap = argparse.ArgumentParser(
        prog="diskhog",
        description="Show the largest directories under PATH and how they changed since the last run.",
        epilog="example: diskhog ~/projects --top 10 --depth 2")
    ap.add_argument("path", nargs="?", default=os.path.expanduser("~"), help="directory to scan (default: your home)")
    ap.add_argument("-n", "--top", type=int, default=15, help="rows to show (default 15)")
    ap.add_argument("-d", "--depth", type=int, default=1, help="how many levels below PATH to list (default 1)")
    ap.add_argument("--db", default=os.path.join(cache, "pysh-diskhog.sqlite"), help="history database (default %(default)s)")
    ap.add_argument("--no-history", action="store_true", help="don't read or write the history database")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.path)
    if not os.path.isdir(root): sys.exit(f"diskhog: {a.path}: not a directory")
    df, total, errors = scan(root, a.depth)
    df = df.sort_values("kb", ascending=False).reset_index(drop=True)

    old, when = ({}, None)
    if not a.no_history:
        db = open_db(a.db)
        old, when = previous(db, root)
        now = time.time()                              # one timestamp for the whole run
        db.executemany("insert into runs values (?, ?, ?, ?)", [(now, root, p, k) for p, k in zip(df.path, df.kb)])
        db.commit(); db.close()

    console = Console()
    t = Table(title=f"{safe(root)}  ({human(total)} total)", title_justify="left")
    t.add_column("size", justify="right"); t.add_column("%", justify="right")
    t.add_column(""); t.add_column("path", overflow="fold")
    if when: t.add_column(f"since {time.strftime('%Y-%m-%d %H:%M', time.localtime(when))}", justify="right")
    top = df.head(a.top)
    for p, kb in zip(top.path, top.kb):
        frac = kb / total if total else 0
        row = [human(kb), f"{frac*100:.0f}", "█" * round(frac * 20), safe(os.path.relpath(p, root)) if p != root else "."]
        if when: row.append(signed(kb - old[p]) if p in old else "new")
        t.add_row(*row)
    console.print(t)
    if len(df) > a.top: console.print(f"[dim]… and {len(df) - a.top} more[/dim]")
    if errors: console.print(f"[dim]{errors} entries could not be read (permissions?)[/dim]")
    if when:
        gone = [p for p in old if p not in set(df.path)]
        if gone: console.print(f"[dim]{len(gone)} directories from the last run no longer exist[/dim]")

if __name__ == "__main__":
    main(sys.argv[1:])

#!/usr/local/bin/pysh
"""gitstats - who changes what, and when, in a git repository.

Commits per author, a weekday/hour activity picture, the files with the most churn, and the biggest
single commits. Everything comes from one `git log --numstat` call.
"""
import argparse, datetime as dt, os, re, sys
import pandas as pd
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

SPARK = " ▁▂▃▄▅▆▇█"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def spark(values):
    top = max(values) or 1
    return "".join(SPARK[round(v / top * 8)] for v in values)

def history(repo, since, rev):
    args = []
    if since: args.append(f"--since={since}")
    log = !(git -C @(repo) log --no-merges --numstat --format=@%x09%H%x09%an%x09%at%x09%s @(args) @(rev))
    if not log:
        sys.exit(f"gitstats: git log failed: {log.err.strip() or log.out.strip()}")
    commits, files, cur = [], [], None
    for line in log.out.splitlines():
        if line.startswith("@\t"):
            _, sha, author, ts, subject = line.split("\t", 4)
            cur = {"sha": sha[:8], "author": author, "when": dt.datetime.fromtimestamp(int(ts)), "subject": subject, "added": 0, "deleted": 0}
            commits.append(cur)
        elif line.strip() and cur is not None:
            add, dele, path = line.split("\t", 2)
            if add == "-": continue                                  # binary file
            cur["added"] += int(add); cur["deleted"] += int(dele)
            files.append({"path": path, "churn": int(add) + int(dele)})
    return pd.DataFrame(commits), pd.DataFrame(files)

def main(argv=None):
    ap = argparse.ArgumentParser(prog="gitstats", description="Statistics about a git repository's history.",
                                 epilog="examples:  gitstats   |   gitstats ~/src/project --since '6 months ago' --top 5")
    ap.add_argument("repo", nargs="?", default=".", help="repository path (default: current directory)")
    ap.add_argument("--since", help="only commits since this date, e.g. '2 weeks ago' or 2025-01-01")
    ap.add_argument("--rev", default="HEAD", help="branch or revision to walk (default HEAD)")
    ap.add_argument("-n", "--top", type=int, default=8, help="rows per table (default 8)")
    a = ap.parse_args(argv)

    probe = !(git -C @(a.repo) rev-parse --show-toplevel)
    if not probe: sys.exit(f"gitstats: {a.repo}: not a git repository")
    top = probe.out.strip()
    commits, files = history(a.repo, a.since, a.rev)
    console = Console()
    if commits.empty:
        console.print("no commits in that range"); return 0

    console.print(f"[bold]{safe(os.path.basename(top))}[/bold]  {len(commits)} commits by {commits.author.nunique()} authors, "
                  f"{commits.when.min():%Y-%m-%d} → {commits.when.max():%Y-%m-%d}, "
                  f"+{commits.added.sum():,} -{commits.deleted.sum():,} lines")

    by = commits.groupby("author").agg(commits=("sha", "count"), added=("added", "sum"), deleted=("deleted", "sum"),
                                       last=("when", "max")).sort_values("commits", ascending=False).head(a.top)
    t = Table(title="authors", title_justify="left")
    for c, j in (("author", "left"), ("commits", "right"), ("+lines", "right"), ("-lines", "right"), ("last commit", "left")): t.add_column(c, justify=j)
    for name, r in by.iterrows(): t.add_row(safe(name), str(r.commits), f"{r.added:,}", f"{r.deleted:,}", f"{r['last']:%Y-%m-%d}")
    console.print(t)

    wd = commits.when.dt.weekday.value_counts().reindex(range(7), fill_value=0)
    hr = commits.when.dt.hour.value_counts().reindex(range(24), fill_value=0)
    console.print("by weekday  " + "  ".join(f"{d} {spark([n])}{n}" for d, n in zip(DAYS, wd)))
    console.print(f"by hour     00h {spark(list(hr))} 23h")

    if not files.empty:
        ch = files.groupby("path").agg(touches=("path", "count"), churn=("churn", "sum")).sort_values("churn", ascending=False).head(a.top)
        t = Table(title="most churn", title_justify="left")
        for c, j in (("file", "left"), ("commits", "right"), ("lines changed", "right")): t.add_column(c, justify=j, overflow="fold")
        for path, r in ch.iterrows(): t.add_row(safe(path), str(r.touches), f"{r.churn:,}")
        console.print(t)

    big = commits.assign(size=commits.added + commits.deleted).sort_values("size", ascending=False).head(a.top)
    t = Table(title="largest commits", title_justify="left")
    for c, j in (("commit", "left"), ("author", "left"), ("lines", "right"), ("subject", "left")): t.add_column(c, justify=j, overflow="fold")
    for r in big.itertuples(): t.add_row(r.sha, safe(r.author), f"{r.size:,}", safe(r.subject[:60]))
    console.print(t)

if __name__ == "__main__":
    main(sys.argv[1:])

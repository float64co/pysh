#!/usr/local/bin/pysh
"""authlog - who is hammering your SSH server?

Reads an auth log (or the journal), finds failed and invalid SSH logins, groups them by source address,
and shows bursts: runs of attempts less than a minute apart. For the worst offenders it PRINTS a
suggested firewall command; it never runs one. With no readable log it falls back to a built-in
sample, so you can see the output anywhere.
"""
import argparse, collections, datetime as dt, ipaddress, os, re, sys
import pandas as pd
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

SAMPLE = """\
2025-03-02T03:14:01+00:00 web sshd[811]: Failed password for root from 203.0.113.9 port 40001 ssh2
2025-03-02T03:14:04+00:00 web sshd[811]: Failed password for root from 203.0.113.9 port 40002 ssh2
2025-03-02T03:14:09+00:00 web sshd[811]: Failed password for invalid user admin from 203.0.113.9 port 40003 ssh2
2025-03-02T03:14:15+00:00 web sshd[811]: Invalid user oracle from 203.0.113.9 port 40004
2025-03-02T03:14:20+00:00 web sshd[811]: Failed password for invalid user test from 203.0.113.9 port 40005 ssh2
2025-03-02T03:14:31+00:00 web sshd[811]: Failed password for root from 203.0.113.9 port 40006 ssh2
2025-03-02T09:40:11+00:00 web sshd[912]: Failed password for ubuntu from 198.51.100.77 port 52000 ssh2
2025-03-02T09:41:12+00:00 web sshd[912]: Failed password for ubuntu from 198.51.100.77 port 52004 ssh2
2025-03-02T11:02:00+00:00 web sshd[977]: Accepted publickey for deploy from 192.0.2.10 port 61000 ssh2
2025-03-02T11:15:00+00:00 web sshd[990]: Failed password for deploy from 192.0.2.10 port 61100 ssh2
2025-03-02T21:00:00+00:00 web sshd[1201]: Failed password for root from 2001:db8::5 port 33000 ssh2
2025-03-02T21:00:03+00:00 web sshd[1201]: Failed password for root from 2001:db8::5 port 33001 ssh2
2025-03-02T21:00:07+00:00 web sshd[1201]: Failed password for invalid user git from 2001:db8::5 port 33002 ssh2
"""

FAIL = re.compile(r"sshd\[\d+\]: (?:Failed \S+ for (?:invalid user )?(?P<u1>\S+)|Invalid user (?P<u2>\S+)) from (?P<ip>[0-9a-fA-F:.]+)")
ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
SYSLOG = re.compile(r"^([A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d:\d\d)")

def stamp(line, year):
    m = ISO.match(line)
    if m: return dt.datetime.fromisoformat(m.group(1))
    m = SYSLOG.match(line)
    if m:
        try: return dt.datetime.strptime(f"{year} {' '.join(m.group(1).split())}", "%Y %b %d %H:%M:%S")
        except ValueError: return None
    return None

def parse(text, year):
    rows = []
    for line in text.splitlines():
        m = FAIL.search(line)
        if not m: continue
        try: ip = str(ipaddress.ip_address(m.group("ip")))                  # only well-formed addresses get any further
        except ValueError: continue
        when = stamp(line, year)
        if when: rows.append((when, ip, m.group("u1") or m.group("u2")))
    return pd.DataFrame(rows, columns=["when", "ip", "user"])

def read_log(path, sample=False):
    """-> (text, where it came from)"""
    if sample: return SAMPLE, "built-in sample"
    if path:
        try:
            with open(path, errors="replace") as f: return f.read(), path
        except OSError as e: sys.exit(f"authlog: {path}: {e.strerror}")
    for p in ("/var/log/auth.log", "/var/log/secure"):
        if os.access(p, os.R_OK):
            with open(p, errors="replace") as f: return f.read(), p
    j = !(journalctl -q --no-pager -o short-iso -u ssh -u sshd -n 20000)
    if j.out.strip(): return j.out, "the systemd journal"
    return SAMPLE, "built-in sample (no readable auth log or journal on this machine)"

def bursts(times, gap):
    """split sorted timestamps into runs where consecutive attempts are <= gap seconds apart -> list of (start, end, count)"""
    out, start, prev, n = [], None, None, 0
    for t in times:
        if prev is None or (t - prev).total_seconds() > gap:
            if n: out.append((start, prev, n))
            start, n = t, 0
        prev, n = t, n + 1
    if n: out.append((start, prev, n))
    return out

def main(argv=None):
    ap = argparse.ArgumentParser(prog="authlog", description="Summarise failed SSH logins by source address, with burst detection.",
                                 epilog="examples:  authlog   |   authlog /var/log/auth.log --min 10   |   sudo authlog --since 2025-03-01")
    ap.add_argument("log", nargs="?", help="log file (default: /var/log/auth.log, /var/log/secure, the journal, then a sample)")
    ap.add_argument("--min", type=int, default=5, dest="threshold", help="suggest a block for addresses with at least this many failures (default 5)")
    ap.add_argument("--gap", type=float, default=60, help="seconds between attempts that still count as one burst (default 60)")
    ap.add_argument("--since", help="only count attempts on or after this date (YYYY-MM-DD)")
    ap.add_argument("-n", "--top", type=int, default=10, help="addresses to list (default 10)")
    ap.add_argument("--sample", action="store_true", help="analyse the built-in sample log instead of a real one")
    ap.add_argument("--year", type=int, default=dt.date.today().year, help="year to assume for old-style 'Mar  2 03:14:01' timestamps")
    a = ap.parse_args(argv)

    text, source = read_log(a.log, a.sample)
    df = parse(text, a.year)
    console = Console()
    console.print(f"[dim]source: {safe(source)}[/dim]")
    if a.since:
        try: df = df[df.when >= pd.Timestamp(a.since)]
        except ValueError: sys.exit(f"authlog: bad --since date {a.since!r} (use YYYY-MM-DD)")
    if df.empty:
        console.print("no failed SSH logins found"); return 0

    console.print(f"[bold]{len(df):,} failed attempts[/bold] from {df.ip.nunique()} addresses, {df.when.min():%Y-%m-%d %H:%M} → {df.when.max():%Y-%m-%d %H:%M}")
    t = Table(title="top sources", title_justify="left")
    for c, j in (("address", "left"), ("tries", "right"), ("bursts", "right"), ("worst burst", "left"), ("users", "right"), ("often targeted", "left")):
        t.add_column(c, justify=j, overflow="fold", no_wrap=(c == "address"))
    suggestions = []
    for ip, g in sorted(df.groupby("ip"), key=lambda kv: -len(kv[1]))[:a.top]:
        bs = bursts(sorted(g.when), a.gap)
        s, e, n = max(bs, key=lambda b: b[2])
        users = g.user.value_counts()
        t.add_row(ip, str(len(g)), str(len(bs)), f"{n} in {max(1, int((e - s).total_seconds()))}s @{s:%H:%M}",
                  str(users.size), ", ".join(safe(u) for u in users.index[:2]), style="bold red" if len(g) >= a.threshold else None)
        if len(g) >= a.threshold: suggestions.append(ip)
    console.print(t)

    users = df.user.value_counts().head(5)
    console.print("most attacked accounts: " + ", ".join(f"{safe(u)} ({n})" for u, n in users.items()))
    hours = df.when.dt.hour.value_counts().reindex(range(24), fill_value=0)
    top = int(hours.max()) or 1
    console.print("by hour of day: " + "".join(" ▁▂▃▄▅▆▇█"[round(n / top * 8)] for n in hours) + "  (00h → 23h)")

    if suggestions:
        console.print(f"\n[bold]suggested blocks[/bold] ({a.threshold}+ failures) — review before running, nothing was changed:")
        for ip in suggestions: console.print(f"  sudo ufw deny from {ip}")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

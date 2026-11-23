#!/usr/local/bin/pysh
"""procwatch - top processes by CPU or memory, a per-user summary, and a runaway warning.

A friendlier, scriptable `top`: it samples CPU over an interval, prints a table, and exits non-zero
if any process is above the warning threshold (handy in cron or a health check).
"""
import argparse, os, re, sys, time
import psutil
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T": return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024

def sample(interval):
    procs = list(psutil.process_iter(["pid", "name", "username", "memory_info"]))
    for p in procs:
        try: p.cpu_percent(None)                       # prime the counter
        except psutil.Error: pass
    time.sleep(interval)
    rows = []
    for p in procs:
        try:
            rows.append({"pid": p.pid, "name": p.info["name"] or "?", "user": p.info["username"] or "?",
                         "cpu": p.cpu_percent(None), "rss": p.info["memory_info"].rss if p.info["memory_info"] else 0,
                         "mem": p.memory_percent()})
        except psutil.Error: pass                      # exited, or not ours to look at
    return rows

def render(rows, a, console):
    host = $(hostname)
    load = " ".join(f"{x:.2f}" for x in os.getloadavg())
    vm = psutil.virtual_memory()
    console.print(f"[bold]{safe(host)}[/bold]  load {load}  cpus {psutil.cpu_count()}  "
                  f"mem {human(vm.used)}/{human(vm.total)} ({vm.percent:.0f}%)")
    key = "cpu" if a.sort == "cpu" else "rss"
    t = Table()
    for c, j in (("pid", "right"), ("user", "left"), ("cpu%", "right"), ("mem", "right"), ("mem%", "right"), ("command", "left")):
        t.add_column(c, justify=j)
    for r in sorted(rows, key=lambda r: r[key], reverse=True)[:a.top]:
        hot = r["cpu"] >= a.warn_cpu
        t.add_row(str(r["pid"]), safe(r["user"][:12]), f"{r['cpu']:.1f}", human(r["rss"]), f"{r['mem']:.1f}", safe(r["name"]),
                  style="bold red" if hot else None)
    console.print(t)
    users = {}
    for r in rows:
        u = users.setdefault(r["user"], [0, 0.0, 0]); u[0] += 1; u[1] += r["cpu"]; u[2] += r["rss"]
    s = Table(title="per user", title_justify="left")
    for c, j in (("user", "left"), ("procs", "right"), ("cpu%", "right"), ("mem", "right")): s.add_column(c, justify=j)
    for name, (n, cpu, rss) in sorted(users.items(), key=lambda kv: kv[1][2], reverse=True)[:5]:
        s.add_row(safe(name), str(n), f"{cpu:.1f}", human(rss))
    console.print(s)

def main(argv=None):
    ap = argparse.ArgumentParser(prog="procwatch", description="Top processes, per-user totals, and a runaway-process check.",
                                 epilog="exit status: 1 if any process used at least --warn-cpu percent of a core, else 0")
    ap.add_argument("-n", "--top", type=int, default=10, help="processes to show (default 10)")
    ap.add_argument("-s", "--sort", choices=("cpu", "mem"), default="cpu", help="sort order (default cpu)")
    ap.add_argument("-i", "--interval", type=float, default=1.0, help="seconds to sample CPU over (default 1)")
    ap.add_argument("-c", "--count", type=int, default=1, help="how many refreshes; 0 keeps going until Ctrl-C (default 1)")
    ap.add_argument("--warn-cpu", type=float, default=90.0, help="flag processes at or above this CPU%% (default 90)")
    a = ap.parse_args(argv)

    console = Console(); hot = False; n = 0
    try:
        while a.count == 0 or n < a.count:
            rows = sample(a.interval)
            if n: console.rule()
            render(rows, a, console)
            hot = hot or any(r["cpu"] >= a.warn_cpu for r in rows)
            n += 1
    except KeyboardInterrupt:
        pass
    if hot: console.print(f"[red]at least one process is using {a.warn_cpu:.0f}% CPU or more[/red]")
    return 1 if hot else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

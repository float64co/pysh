#!/usr/local/bin/pysh
"""fleet - run a check on many machines at once and put the answers in one table.

With host names, runs a command on each over ssh (in parallel, never prompting for a password). With
none, it runs a handful of checks on this machine, so you can see it work. Exits 1 if anything failed.
"""
import argparse, re, subprocess, sys, time
from pyshlib import in_env, par, table

LOCAL = {"uptime": "uptime", "kernel": "uname -r", "root-disk": "df -h --output=pcent,avail /", "users": "who -q"}

def clean(s):
    """what a remote machine prints is untrusted: no terminal escapes get through"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", s)

def timed(run, target):
    start = time.time()
    r = run(target)
    lines = [l for l in (r.out or "").splitlines() if l.strip()] or [l for l in (r.err or "").splitlines() if l.strip()]
    return {"target": target, "rc": r.returncode, "secs": time.time() - start, "lines": lines}

def local_check(name):
    return sh(LOCAL[name], timeout=a.timeout)

def remote_check(host):
    # host and command are each one argument, whatever they contain
    try: return sh("ssh -o BatchMode=yes -o ConnectTimeout=@(str(int(a.connect_timeout))) -- @(host) @(a.command)", timeout=a.timeout)
    except subprocess.TimeoutExpired: return Result(124, "", f"no answer within {a.timeout:g}s")

ap = argparse.ArgumentParser(prog="fleet", description="Run a command on many hosts in parallel and tabulate the results.",
                             epilog="examples:  fleet   |   fleet web1 web2 db1 -c 'df -h /'   |   fleet -f hosts.txt -j 20 --full")
ap.add_argument("hosts", nargs="*", help="hosts to ssh to (default: check this machine)")
ap.add_argument("-f", "--file", help="read more hosts from FILE, one per line ('#' starts a comment)")
ap.add_argument("-c", "--command", default="uptime", help="command to run on each host (default: uptime)")
ap.add_argument("-j", "--jobs", type=int, default=8, help="how many at once (default 8)")
ap.add_argument("-t", "--timeout", type=float, default=30, help="give up on one target after this many seconds (default 30)")
ap.add_argument("--connect-timeout", type=float, default=5, help="ssh connection timeout in seconds (default 5)")
ap.add_argument("--full", action="store_true", help="also print every line each target returned")
a = ap.parse_args()
if a.jobs < 1: ap.error("--jobs must be at least 1")

hosts = list(a.hosts)
if a.file:
    try:
        with open(a.file) as f: hosts += [l.split("#")[0].strip() for l in f if l.split("#")[0].strip()]
    except OSError as e: sys.exit(f"fleet: {a.file}: {e.strerror}")
bad = [h for h in hosts if h.startswith("-") or any(c.isspace() for c in h)]
if bad: ap.error(f"not a host name: {bad[0]!r}")
if a.file and not hosts: sys.exit("fleet: no hosts in that file")

targets, run = (hosts, remote_check) if hosts else (list(LOCAL), local_check)
# LC_ALL=C: the same wording and number formats from every machine that accepts it
with in_env(LC_ALL="C"):
    results = par(targets, lambda t: timed(run, t), jobs=a.jobs)

rows = ["TARGET STATUS SECONDS ANSWER"]
for r in results:
    answer = clean(r["lines"][0].strip()) if r["lines"] else "-"
    if len(r["lines"]) > 1: answer += f"  (+{len(r['lines']) - 1} more)"
    rows.append(f"{r['target'].replace(' ', '_')} {'ok' if r['rc'] == 0 else 'exit-' + str(r['rc'])} {r['secs']:.2f} {answer}")
@(rows) | @table

if a.full:
    for r in results:
        print(f"\n== {clean(r['target'])}")
        for line in r["lines"]: print("  " + clean(line))

failed = sum(r["rc"] != 0 for r in results)
if failed: print(f"\n{failed} of {len(results)} failed", file=sys.stderr)
sys.exit(1 if failed else 0)

#!/usr/local/bin/pysh
"""inventory - a one-screen report on this machine, built from Python pipeline stages.

Shows how much memory each user's processes hold, the biggest processes, the fullest file systems,
and optionally which processes match a name. Commands feed Python (`ps ... | procs=@parse_ps`) and
Python feeds commands (`@(rows) | sort | head`).
"""
import argparse, collections, re, sys

def clean(s):
    """process and mount names are chosen by others: no terminal escapes get through"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", s)

def human(kb):
    n = float(kb)
    for unit in ("K", "M", "G", "T"):
        if n < 1024 or unit == "T": return f"{n:.1f}{unit}"
        n /= 1024

def parse_ps(stream):
    """`user pid rss command` lines -> dicts"""
    for line in stream:
        f = line.split(None, 3)
        if len(f) == 4 and f[1].isdigit() and f[2].isdigit():
            yield {"user": clean(f[0]), "pid": int(f[1]), "rss": int(f[2]), "name": clean(f[3].strip())}

def parse_df(stream):
    """`df -P` -> (percent used, mount point), fullest first"""
    rows = []
    for line in list(stream)[1:]:
        f = line.split(None, 5)
        if len(f) == 6 and f[4].rstrip("%").isdigit():
            rows.append((int(f[4].rstrip("%")), clean(f[5].strip())))
    return sorted(rows, reverse=True)

ap = argparse.ArgumentParser(prog="inventory", description="Summarise this machine's processes and disks.",
                             epilog="examples:  inventory   |   inventory --top 10 --find python")
ap.add_argument("-n", "--top", type=int, default=5, help="rows per section (default 5)")
ap.add_argument("--find", metavar="TEXT", help="also list processes whose name contains TEXT (case-insensitive)")
a = ap.parse_args()
if a.top < 1: ap.error("--top must be at least 1")

today = ""
date '+%A %d %B %Y' | today=@
print(f"== inventory, {today.strip()}")

procs = []
ps -eo user:32,pid,rss,comm --no-headers | procs=@parse_ps

totals = collections.Counter()
for p in procs: totals[p["user"]] += p["rss"]
by_user = [f"{kb}\t{user}" for user, kb in totals.items()]
top_users = []
@(by_user) | sort -rn | head -n @(str(a.top)) | top_users=@lines

print(f"\n== memory by user ({len(procs)} processes)")
for line in top_users:
    kb, user = line.split("\t")
    print(f"  {human(int(kb)):>9}  {user}")

print("\n== biggest processes")
for p in sorted(procs, key=lambda p: p["rss"], reverse=True)[:a.top]:
    print(f"  {human(p['rss']):>9}  {p['pid']:>7}  {p['user']:<12} {p['name']}")

disks = None
df -P 2>/dev/null | disks=@parse_df
print("\n== fullest file systems")
if disks is None: print("  df could not read every file system")
else:
    for used, mount in disks[:a.top]: print(f"  {used:>3}%  {mount}")

if a.find:
    # inside an indented block a shell line is written $[...]; grep finding nothing gives an empty list, not a failure
    $[ps -eo comm --no-headers | grep -i -F -- @(a.find) | found=@lines]
    print(f"\n== processes matching {a.find!r}: {len(found)}")
    for name, count in collections.Counter(clean(n) for n in found).most_common(a.top):
        print(f"  {count:>4} x {name}")
sys.exit(0)

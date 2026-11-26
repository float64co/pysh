#!/usr/local/bin/pysh
"""histreport - what do you run, where, and what keeps failing?

Reads the record interactive pysh shells keep (command, time, directory, exit status) through
pyshlib's `hist`. Nothing is changed and nothing leaves this machine. Note that the report shows
your own commands, so mind who is looking.
"""
import argparse, collections, json, os, random, re, shutil, sys, tempfile, time
from pyshlib import freq, hist, spark

def first_word(command):
    """the program a command line ran, skipping `X=1` settings and `sudo`"""
    for word in command.split():
        if word == "sudo" or ("=" in word and not word.startswith("-") and word.split("=")[0].isidentifier()): continue
        return word
    return command.strip()

def demo_record(directory):
    """a made-up record, so the report can be seen anywhere"""
    rng = random.Random(5)
    home = os.path.expanduser("~")
    commands = [("git status", 0), ("ls -l", 0), ("make test", 2), ("git commit", 0), ("cd src", 0), ("pytest -x", 1), ("gti push", 127)]
    base = os.path.join(directory, "history")
    with open(base + ".jsonl", "w") as f:
        for i in range(240):
            command, status = rng.choices(commands, weights=(30, 25, 8, 10, 12, 7, 2))[0]
            f.write(json.dumps({"cmd": command, "time": time.time() - rng.random() * 20 * 86400,
                                "cwd": os.path.join(home, rng.choice(["src/app", "src/lib", "notes"])), "status": status}) + "\n")
    return base

ap = argparse.ArgumentParser(prog="histreport", description="Summarise your pysh history: commands, directories, failures, hours.",
                             epilog="examples:  histreport   |   histreport --days 7 --here   |   histreport --demo")
ap.add_argument("--days", type=float, help="only the last N days")
ap.add_argument("--here", action="store_true", help="only commands run in the current directory")
ap.add_argument("-n", "--top", type=int, default=8, help="rows per table (default 8)")
ap.add_argument("--demo", action="store_true", help="report on a made-up history instead of yours")
a = ap.parse_args()
if a.top < 1: ap.error("--top must be at least 1")

scratch = None
if a.demo:
    scratch = tempfile.mkdtemp(prefix="histreport-")
    shell.HIST = demo_record(scratch)                      # `shell` is pysh itself; hist reads next to its history file
try:
    entries = hist.search(since=None if a.days is None else a.days * 86400, cwd="." if a.here else None)
finally:
    if scratch: shutil.rmtree(scratch)

if not entries:
    print("nothing recorded yet for that selection.")
    print("pysh records commands as you use it interactively; try --demo to see the report.")
    sys.exit(0)

failed = [e for e in entries if e.status]
print(f"{len(entries):,} commands, {entries[0].time:%Y-%m-%d} to {entries[-1].time:%Y-%m-%d}; "
      f"{len(failed):,} failed ({len(failed) / len(entries):.0%})")

words = [first_word(e.cmd) for e in entries]
print("\n== what you run")
@(words) | @freq(a.top)

home = os.path.expanduser("~")
places = ["~" + e.cwd[len(home):] if e.cwd == home or e.cwd.startswith(home + "/") else e.cwd for e in entries]
print("\n== where")
@(places) | @freq(a.top)

if failed:
    print("\n== what keeps failing")
    print(freq([" ".join(e.cmd.split())[:60] for e in failed], a.top))
    unknown = sorted({first_word(e.cmd) for e in failed if e.status == 127})
    if unknown: print("not found as commands (typos?): " + ", ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", w) for w in unknown[:10]))

hours = collections.Counter(e.time.hour for e in entries)
print("\n== by hour of day")
print(f"00h {spark([hours.get(h, 0) for h in range(24)])} 23h")

#!/usr/local/bin/pysh
"""logwatch - follow a log, keep only the lines that matter, and count them by level.

The whole job is one pipeline of Python stages, `@source | @matching | @report`, so lines flow through
as they arrive. With no file it watches a built-in demonstration log. Press Ctrl-C to stop following
and still get the summary.
"""
import argparse, collections, os, random, re, sys, time

LEVELS = ("ERROR", "WARN", "INFO", "DEBUG")
COLOURS = {"ERROR": "31", "WARN": "33", "INFO": "32", "DEBUG": "90"}

def clean(s):
    """log lines are written by others: no terminal escapes get through"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", s)

def demo_log(count, delay):
    rng = random.Random(7)
    events = {"INFO": ["request served", "user signed in", "cache warmed"], "DEBUG": ["pool size 8", "gc cycle"],
              "WARN": ["slow query", "retrying upstream"], "ERROR": ["connection reset by peer", "disk quota exceeded"]}
    for i in range(count):
        level = rng.choices(LEVELS, weights=(2, 3, 10, 4))[0]
        yield f"12:00:{i % 60:02d} {level:5} {rng.choice(events[level])}\n"
        if delay: time.sleep(delay)

def source():
    """first stage: the lines of the file (followed with -f), or the demo log"""
    if a.file is None:
        yield from demo_log(a.lines, 0.05 if a.follow else 0)
        return
    follow = ["-F"] if a.follow else []
    for line in ![tail -n @(str(a.lines)) @(follow) -- @(a.file)]:
        yield line + "\n"

def matching(stream):
    wanted = re.compile(a.pattern, re.I) if a.pattern else None
    rank = {name: i for i, name in enumerate(LEVELS)}
    for line in stream:
        level = next((name for name in LEVELS if name in line), None)
        if a.level and (level is None or rank[level] > rank[a.level]): continue
        if wanted and not wanted.search(line): continue
        yield line

def report(stream):
    """last stage, so it runs in the shell: show each line as it comes, then the tally"""
    counts = collections.Counter()
    colour = sys.stdout.isatty()
    try:
        for line in stream:
            level = next((name for name in LEVELS if name in line), "other")
            counts[level] += 1
            text = clean(line.rstrip("\n"))
            print(f"\x1b[{COLOURS[level]}m{text}\x1b[0m" if colour and level in COLOURS else text, flush=True)
    except KeyboardInterrupt:
        print()
    summary = "  ".join(f"{name} {counts[name]}" for name in (*LEVELS, "other") if counts[name])
    print(f"-- {sum(counts.values())} lines: {summary or 'nothing matched'}")

ap = argparse.ArgumentParser(prog="logwatch", description="Filter a log as it grows and count the lines by level.",
                             epilog="examples:  logwatch   |   logwatch /var/log/syslog -f -p ssh   |   logwatch app.log -l WARN -n 5000")
ap.add_argument("file", nargs="?", help="log file (default: a generated demonstration log)")
ap.add_argument("-f", "--follow", action="store_true", help="keep watching for new lines (Ctrl-C stops)")
ap.add_argument("-p", "--pattern", help="only lines matching this regular expression (case-insensitive)")
ap.add_argument("-l", "--level", choices=LEVELS, help="only this severity or worse")
ap.add_argument("-n", "--lines", type=int, default=40, help="how many existing lines to start with (default 40)")
a = ap.parse_args()

if a.file and not os.access(a.file, os.R_OK): sys.exit(f"logwatch: cannot read {a.file}")
if a.lines < 0: ap.error("--lines can't be negative")
try: re.compile(a.pattern or "")
except re.error as e: ap.error(f"bad --pattern: {e}")

@source | @matching | @report

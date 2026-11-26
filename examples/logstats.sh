#!/usr/local/bin/pysh
"""logstats - who is calling, what they get back, and how slow it is.

Reads a web access log (the common/combined format, optionally with the response time in ms as the
last field) or a JSON-lines log with "ip", "status", "path" and "ms" keys. With no file it makes a
demonstration log. Most of the work is pyshlib stages in ordinary pipelines.
"""
import argparse, os, random, sys, tempfile
from pyshlib import fields, freq, jsonl, spark, stats

def demo_log(path, lines=600):
    rng = random.Random(11)
    clients = ["10.0.0.5", "10.0.0.9", "192.168.1.20", "203.0.113.7", "198.51.100.4"]
    paths = ["/", "/api/users", "/api/orders", "/health", "/static/app.js", "/login"]
    with open(path, "w") as f:
        for i in range(lines):
            ip = rng.choices(clients, weights=(30, 18, 9, 4, 2))[0]
            status = rng.choices([200, 304, 404, 500, 302], weights=(70, 12, 10, 3, 5))[0]
            slow = 1 + 4 * (i > lines * 0.7)                                      # things get slower near the end
            ms = round(rng.lognormvariate(3, 0.5) * slow)
            f.write(f'{ip} - - [25/Nov/2026:10:{i // 60 % 60:02d}:{i % 60:02d} +0000] "GET {rng.choice(paths)} HTTP/1.1" {status} {rng.randint(100, 9000)} {ms}\n')

def trend(values, width=60):
    """average the values into at most `width` buckets, as a one-line chart"""
    if not values: return ""
    size = max(1, -(-len(values) // width))
    return str(spark([sum(chunk) / len(chunk) for chunk in (values[i:i + size] for i in range(0, len(values), size))]))

def number(text):
    try: return float(text)
    except (TypeError, ValueError): return None

def from_access_log(path):
    return {"lines": !(cat -- @(path) | @(lambda f: sum(1 for _ in f))).value,
            "clients": !(cat -- @(path) | @fields 1 | @freq(a.top)).value,
            "statuses": !(cat -- @(path) | @fields 9 | @freq).value,
            "paths": !(cat -- @(path) | @fields 7 | @freq(a.top)).value,
            "ms": [v for v in map(number, !(cat -- @(path) | @fields -1 | @(lambda f: f.read().split("\n"))).value) if v is not None]}

def from_json_log(path):
    rows = !(cat -- @(path) | @jsonl).value
    rows = [r for r in rows if isinstance(r, dict)]
    column = lambda key: [str(r[key]) for r in rows if key in r]
    return {"lines": len(rows), "clients": freq(column("ip"), a.top), "statuses": freq(column("status")),
            "paths": freq(column("path"), a.top), "ms": [v for v in (number(r.get("ms")) for r in rows) if v is not None]}

ap = argparse.ArgumentParser(prog="logstats", description="Summarise a web access log or a JSON-lines log.",
                             epilog="examples:  logstats   |   logstats /var/log/nginx/access.log --top 20   |   logstats app.jsonl")
ap.add_argument("file", nargs="?", help="log file (default: a generated demonstration log)")
ap.add_argument("-n", "--top", type=int, default=5, help="rows per table (default 5)")
ap.add_argument("--format", choices=("auto", "access", "json"), default="auto", help="log format (default: auto, by the first line)")
a = ap.parse_args()
if a.top < 1: ap.error("--top must be at least 1")

demo = None
if a.file is None:
    fd, demo = tempfile.mkstemp(suffix=".log"); os.close(fd); demo_log(demo)
    print("no file given: using a generated demonstration log")
path = a.file or demo
try:
    with open(path, errors="replace") as f: first = next((l for l in f if l.strip()), "")
except OSError as e: sys.exit(f"logstats: {path}: {e.strerror}")
if not first: sys.exit(f"logstats: {path}: empty")
kind = a.format if a.format != "auto" else ("json" if first.lstrip().startswith("{") else "access")

try:
    got = from_json_log(path) if kind == "json" else from_access_log(path)
finally:
    if demo: os.unlink(demo)

print(f"{got['lines']:,} lines ({kind} format)")
for title, key in (("busiest clients", "clients"), ("status codes", "statuses"), ("most requested", "paths")):
    print(f"\n== {title}")
    print(got[key] if len(got[key]) else "  (nothing to count)")

print("\n== response time, ms")
if got["ms"]:
    print(stats([str(v) for v in got["ms"]]))
    print(f"over the file: {trend(got['ms'])}")
else:
    print("  (no timings in this log)")

errors = sum(count for status, count in got["statuses"] if str(status).startswith("5"))
if errors: print(f"\n{errors} server errors (5xx)")

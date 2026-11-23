#!/usr/local/bin/pysh
"""flaky - run a command many times and find out whether it is flaky.

Runs COMMAND N times, several at once, then reports the pass rate, the exit codes seen, how long
runs took, and the error lines that show up most often (digits are masked so "timeout after 31ms"
and "timeout after 28ms" count as the same failure). With no command it runs a built-in
demonstration that fails about a third of the time.
"""
import argparse, collections, concurrent.futures as cf, re, shlex, statistics, subprocess, sys


def clean(s):
    """text from outside made safe to print: control characters (terminal escapes) become '?', newlines and tabs are kept"""
    return re.sub(r"[\x00-\x08\x0b-\x0c\x0d-\x1f\x7f-\x9f]", "?", str(s))

DEMO = ["python3", "-c", """
import random, sys
r = random.random()
if r < 0.15: print("ERROR: connection reset by peer after %dms" % random.randint(5, 90), file=sys.stderr); sys.exit(1)
if r < 0.30: print("AssertionError: expected 3 items, got %d" % random.randint(0, 2), file=sys.stderr); sys.exit(1)
print("ok")
"""]

def run_once(cmd, timeout):
    try:
        r = sh(cmd, timeout=timeout)
        text = (r.err or "") + (r.out or "")
        if r.returncode == 127 and not text.strip(): text = "command not found"
        return {"rc": r.returncode, "secs": r.duration, "text": text}
    except subprocess.TimeoutExpired:
        return {"rc": "timeout", "secs": timeout, "text": f"timed out after {timeout:g}s"}

def signature(text):
    """the line that best explains a failure, digits masked"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    pick = next((l for l in reversed(lines) if re.search(r"error|fail|exception|assert|timeout|timed out|denied|refused", l, re.I)),
                lines[-1] if lines else "(no output)")
    return clean(re.sub(r"\d+", "N", pick))[:100]

def main(argv=None):
    ap = argparse.ArgumentParser(prog="flaky", description="Run a command repeatedly to measure how flaky it is.",
                                 usage="flaky [-h] [-n RUNS] [-j JOBS] [-t SECONDS] [--] [COMMAND ...]",
                                 epilog="examples:  flaky -n 50 -j 8 -- pytest -x tests/test_net.py   |   flaky -n 20 -- ./build.sh   |   flaky  (demo)")
    ap.add_argument("-n", "--runs", type=int, default=30, help="how many times to run it (default 30)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="how many runs at once (default 4)")
    ap.add_argument("-t", "--timeout", type=float, default=60, help="kill a run after this many seconds (default 60)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="the command to run (use -- before it if it has options)")
    a = ap.parse_args(argv)

    words = [w for i, w in enumerate(a.command) if not (i == 0 and w == "--")]
    demo = not words
    cmd = shlex.join(DEMO if demo else words)
    if demo: print("no command given: running a built-in demo that fails about 30% of the time\n")
    print(f"{a.runs} runs, {a.jobs} at a time: {cmd if not demo else 'python3 -c <demo>'}")

    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        results = list(pool.map(lambda _: run_once(cmd, a.timeout), range(a.runs)))

    fails = [r for r in results if r["rc"] != 0]
    rate = len(fails) / len(results)
    secs = [r["secs"] for r in results]
    print(f"\npassed {len(results) - len(fails)}/{len(results)}   failed {len(fails)} ({rate:.0%})")
    codes = collections.Counter(str(r["rc"]) for r in results)
    print("exit codes: " + "  ".join(f"{c}×{n}" for c, n in sorted(codes.items())))
    print(f"duration:   median {statistics.median(secs):.2f}s   max {max(secs):.2f}s")
    if fails:
        print("\nmost common failures:")
        for sig, n in collections.Counter(signature(r["text"]) for r in fails).most_common(5):
            print(f"  {n:>3} ×  {sig}")
    verdict = ("stable: passed every time" if not fails else
               "BROKEN: failed every time" if len(fails) == len(results) else
               f"FLAKY: fails {rate:.0%} of the time")
    print(f"\nverdict: {verdict}")
    if fails and len(fails) < len(results) and len(results) < 20:
        print("         (fewer than 20 runs: a low failure rate may not show up; try -n 100)")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

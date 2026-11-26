#!/usr/bin/env python3
"""Run the test suite in parallel, one test per worker at a time, and say where the time went.

    python3 tests/run.py                 # everything
    python3 tests/run.py -j 1            # one at a time
    python3 tests/run.py -k Parser       # tests whose name contains this (repeatable)
    python3 tests/run.py --durations 40  # how many of the slowest tests to list
"""
import argparse, collections, concurrent.futures, os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

def every_test(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from every_test(item)
        else: yield item

def run_one(name):
    """in a worker -> (name, seconds, outcome, details)"""
    result = unittest.TestResult()
    result.buffer = True                                   # keep what tests print out of the report
    start = time.monotonic()
    unittest.defaultTestLoader.loadTestsFromName(name).run(result)
    seconds = time.monotonic() - start
    for outcome, found in (("ERROR", result.errors), ("FAIL", result.failures)):
        if found: return name, seconds, outcome, "\n".join(text for _, text in found)
    if result.unexpectedSuccesses: return name, seconds, "FAIL", "unexpected success"
    if result.skipped: return name, seconds, "skip", result.skipped[0][1]
    return name, seconds, "ok", ""

def main():
    ap = argparse.ArgumentParser(description="Run the tests in parallel and report the slowest.")
    ap.add_argument("-j", "--jobs", type=int, default=min(16, os.cpu_count() or 1), help="tests at once (default %(default)s)")
    ap.add_argument("-k", action="append", default=[], help="only tests whose name contains this")
    ap.add_argument("--durations", type=int, default=15, help="list this many of the slowest (default %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true", help="a line for every test")
    a = ap.parse_args()
    if a.jobs < 1: ap.error("--jobs must be at least 1")

    names = []
    for test in every_test(unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)):
        if type(test).__module__ == "unittest.loader":      # a test file that wouldn't import
            unittest.TextTestRunner().run(test); return 1
        if not a.k or any(k in test.id() for k in a.k): names.append(test.id())
    if not names: print("no tests selected"); return 1

    start, results = time.monotonic(), []
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=a.jobs)
    try:
        pending = {pool.submit(run_one, name): name for name in names}
        for done in concurrent.futures.as_completed(pending):
            try: result = done.result()
            except Exception as e: result = pending[done], 0.0, "ERROR", f"the worker failed: {e!r}"
            results.append(result)
            name, seconds, outcome, details = result
            if a.verbose: print(f"{outcome:5} {seconds:6.2f}s {name}", flush=True)
            elif outcome in ("ERROR", "FAIL"): print(f"\n{outcome}: {name}", flush=True)
            if sys.stderr.isatty(): print(f"\r{len(results)}/{len(names)} ", end="", file=sys.stderr, flush=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    wall = time.monotonic() - start

    bad = sorted(r for r in results if r[2] in ("ERROR", "FAIL"))
    for name, _, outcome, details in bad:
        print(f"\n{'=' * 70}\n{outcome}: {name}\n{'-' * 70}\n{details}")
    per_file = collections.Counter()
    for name, seconds, _, _ in results: per_file[name.split(".")[0]] += seconds
    if a.durations > 0:
        print("\nseconds by file (summed over its tests):")
        for module, seconds in per_file.most_common(): print(f"  {seconds:7.1f}  {module}")
        print("\nslowest tests:")
        for name, seconds, _, _ in sorted(results, key=lambda r: -r[1])[:a.durations]: print(f"  {seconds:7.2f}  {name}")
    skipped = sum(r[2] == "skip" for r in results)
    print(f"\nRan {len(results)} tests in {wall:.1f}s with {a.jobs} at once "
          f"({sum(per_file.values()):.1f}s of test time): {len(bad)} failed, {skipped} skipped")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())

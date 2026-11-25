#!/usr/local/bin/pysh
"""apipeek - fetch JSON with curl and see what shape it is.

One line does the fetching, keeping the raw reply and parsing it:  curl ... | raw=@ | data=@as_json
If curl fails, neither variable is set; if the reply isn't JSON, `raw` still holds what came back. With no URL it reads a built-in sample through curl's file:// support, so it works offline.
"""
import argparse, json, os, re, sys, tempfile

SAMPLE = {"service": "demo", "ok": True, "count": 3,
          "items": [{"id": 1, "name": "alpha", "tags": ["a"]}, {"id": 2, "name": "beta", "tags": []},
                    {"id": 3, "name": "gamma", "tags": ["a", "b"]}],
          "owner": {"login": "someone", "site": None}}

NOT_JSON = object()

def as_json(stream):
    """json.load, but quiet about bad input: the caller says what went wrong"""
    try: return json.load(stream)
    except ValueError: return NOT_JSON

def clean(s):
    """the reply comes from somewhere else: no terminal escapes get through"""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s))

def brief(value):
    if isinstance(value, dict): return f"object with {len(value)} keys"
    if isinstance(value, list): return f"list of {len(value)}"
    text = json.dumps(value)
    return clean(text if len(text) <= 60 else text[:57] + "...")

def describe(value, depth, indent="  "):
    if isinstance(value, list):
        if value and depth > 0:
            print(f"{indent}[0] {brief(value[0])}"); describe(value[0], depth - 1, indent + "  ")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{indent}{clean(key)}: {brief(item)}")
            if isinstance(item, (dict, list)) and depth > 0: describe(item, depth - 1, indent + "  ")

def follow(value, path):
    """`items.0.name` -> value["items"][0]["name"]"""
    for part in path.split("."):
        try: value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            sys.exit(f"apipeek: no {part!r} at that point in the reply")
    return value

ap = argparse.ArgumentParser(prog="apipeek", description="Fetch a JSON document and describe its structure.",
                             epilog="examples:  apipeek   |   apipeek https://api.github.com/repos/python/cpython -d 1   |   apipeek --path items.0.name")
ap.add_argument("url", nargs="?", help="http, https or file URL (default: a built-in sample)")
ap.add_argument("-d", "--depth", type=int, default=2, help="how many levels to open up (default 2)")
ap.add_argument("--path", help="print just the value at this dotted path, as JSON")
ap.add_argument("--max-time", type=float, default=10, help="give up after this many seconds (default 10)")
a = ap.parse_args()

url = a.url
if url is not None and not re.match(r"(https?|file)://", url): ap.error("the URL must start with http://, https:// or file://")
if url is None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f: json.dump(SAMPLE, f)
    sample_file, url = f.name, "file://" + f.name
else:
    sample_file = None

raw, data = None, NOT_JSON
curl -sS -f -L --max-time @(str(a.max_time)) -- @(url) | raw=@ | data=@as_json
status = last.returncode
if sample_file: os.unlink(sample_file)

if raw is None: sys.exit(f"apipeek: could not fetch (curl status {status})")
if data is NOT_JSON:
    sys.exit(f"apipeek: the reply is not JSON ({len(raw)} bytes), starting: {clean(raw[:120])!r}")

if a.path:
    print(json.dumps(follow(data, a.path), indent=2, ensure_ascii=True))
else:
    print(f"{len(raw):,} bytes: {brief(data)}")
    describe(data, a.depth)

#!/usr/local/bin/pysh
"""hashsweep - triage the files under a directory.

Hashes every file, measures its entropy, and reports: duplicates, files whose hash is on an IOC
list you supply, and unusually high-entropy files (compressed, encrypted or packed data) that
aren't an obviously compressed format. Read-only; it never modifies or runs anything it finds.
"""
import argparse, collections, hashlib, math, os, re, sys
import numpy as np
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

KNOWN_COMPRESSED = {".zip", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".mp4",
                    ".mkv", ".avi", ".mov", ".ogg", ".flac", ".pdf", ".whl", ".jar", ".docx", ".xlsx", ".pptx", ".woff", ".woff2", ".pyc"}

def entropy(data):
    """Shannon entropy in bits per byte: 0 (constant) to 8 (random)."""
    if not data: return 0.0
    p = np.bincount(np.frombuffer(data, dtype=np.uint8), minlength=256) / len(data)
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())

def files(root, skip_hidden):
    for d, dirs, names in os.walk(root, followlinks=False):
        if skip_hidden: dirs[:] = [x for x in dirs if not x.startswith(".")]
        dirs.sort()
        for n in sorted(names):
            if skip_hidden and n.startswith("."): continue
            p = os.path.join(d, n)
            if os.path.islink(p) or not os.path.isfile(p): continue
            yield p

def examine(path, algo, sample):
    h = hashlib.new(algo); ent_data = b""; size = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk); size += len(chunk)
            if len(ent_data) < sample: ent_data += chunk[:sample - len(ent_data)]
    return h.hexdigest(), size, entropy(ent_data)

def describe(path):
    r = !(file -b @(path))                        # what does `file` think it is?
    return r.out.strip()[:50] if r else ""

def load_iocs(path):
    try:
        with open(path) as f:
            return {l.split()[0].lower() for l in f if l.strip() and not l.lstrip().startswith("#")}
    except OSError as e:
        sys.exit(f"hashsweep: {path}: {e.strerror}")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="hashsweep", description="Hash, deduplicate and entropy-check the files under a directory.",
                                 epilog="IOC file format: one hash per line (anything after it is ignored); '#' lines are comments.")
    ap.add_argument("path", nargs="?", default=".", help="directory to sweep (default: current directory)")
    ap.add_argument("--algo", default="sha256", choices=sorted(a for a in ("md5", "sha1", "sha256", "sha512")), help="hash algorithm (default sha256)")
    ap.add_argument("--ioc", metavar="FILE", help="file of known-bad hashes to match against (use the same --algo)")
    ap.add_argument("--entropy", type=float, default=7.5, help="flag files above this many bits/byte (default 7.5)")
    ap.add_argument("--min-size", type=int, default=4096, help="ignore files smaller than this for the entropy check (default 4096)")
    ap.add_argument("--max-files", type=int, default=20000, help="stop after this many files (default 20000)")
    ap.add_argument("--hidden", action="store_true", help="include dot files and dot directories")
    ap.add_argument("-n", "--top", type=int, default=10, help="rows per section (default 10)")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.path): sys.exit(f"hashsweep: {a.path}: not a directory")
    iocs = load_iocs(a.ioc) if a.ioc else set()
    by_hash = collections.defaultdict(list); rows = []; skipped = 0; total = 0
    for p in files(a.path, not a.hidden):
        if len(rows) >= a.max_files: print(f"stopped after {a.max_files} files (--max-files)", file=sys.stderr); break
        try: digest, size, ent = examine(p, a.algo, 1 << 20)
        except OSError: skipped += 1; continue
        rows.append((p, digest, size, ent)); by_hash[digest].append((p, size)); total += size

    console = Console()
    console.print(f"[bold]{len(rows):,} files[/bold], {total / 1e6:.1f} MB under {safe(os.path.abspath(a.path))}"
                  + (f"  [dim]({skipped} unreadable)[/dim]" if skipped else ""))

    hits = [(p, d) for p, d, _, _ in rows if d in iocs]
    if iocs:
        if hits:
            t = Table(title=f"IOC MATCHES ({len(hits)})", title_justify="left", style="red")
            t.add_column("file", overflow="fold"); t.add_column(a.algo, overflow="fold")
            for p, d in hits: t.add_row(safe(p), d)
            console.print(t)
        else: console.print(f"[green]no files match the {len(iocs)} hashes in {safe(a.ioc)}[/green]")

    dups = sorted(((v, h) for h, v in by_hash.items() if len(v) > 1 and v[0][1] > 0), key=lambda x: -(len(x[0]) - 1) * x[0][0][1])
    if dups:
        wasted = sum((len(v) - 1) * v[0][1] for v, _ in dups)
        t = Table(title=f"duplicates: {len(dups)} sets, {wasted / 1e6:.1f} MB wasted", title_justify="left")
        t.add_column("copies", justify="right"); t.add_column("size", justify="right"); t.add_column("files", overflow="fold")
        for v, _ in dups[:a.top]: t.add_row(str(len(v)), f"{v[0][1]:,}", "\n".join(safe(p) for p, _ in v[:4]) + (f"\n… +{len(v) - 4}" if len(v) > 4 else ""))
        console.print(t)
    else: console.print("no duplicate files")

    hot = sorted((r for r in rows if r[3] >= a.entropy and r[2] >= a.min_size and os.path.splitext(r[0])[1].lower() not in KNOWN_COMPRESSED),
                 key=lambda r: -r[3])
    if hot:
        t = Table(title="high entropy, not a known compressed format", title_justify="left")
        t.add_column("bits/byte", justify="right"); t.add_column("size", justify="right"); t.add_column("file", overflow="fold"); t.add_column("file(1) says", overflow="fold")
        for p, _, size, ent in hot[:a.top]: t.add_row(f"{ent:.2f}", f"{size:,}", safe(p), safe(describe(p)))
        console.print(t)
    else: console.print(f"no unexplained files above {a.entropy} bits/byte")
    return 1 if hits else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

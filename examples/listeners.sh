#!/usr/local/bin/pysh
"""listeners - what is listening on this machine, and who can reach it?

Parses `ss` into a pandas table and classifies every socket: loopback only, all interfaces, or one
specific address. Run it as root to see the owning process of every socket.
"""
import argparse, re, sys
import pandas as pd
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

def exposure(addr):
    a = addr.strip("[]").split("%")[0]
    if a.startswith("127.") or a == "::1": return "loopback"
    if a in ("0.0.0.0", "::", "*"): return "ALL"
    return "address"

def listeners():
    r = !(ss -H -tulnp)
    if r.returncode != 0 and not r.out:
        sys.exit(f"listeners: ss failed: {r.err.strip()}")
    rows = []
    for line in r.out.splitlines():
        f = line.split(None, 6)
        if len(f) < 6: continue
        proto, local = f[0], f[4]
        host, _, port = local.rpartition(":")
        who = f[6] if len(f) > 6 else ""
        m = re.search(r'\("([^"]+)",pid=(\d+)', who)
        rows.append({"proto": proto, "address": host or "*", "port": int(port) if port.isdigit() else 0,
                     "exposure": exposure(host or "*"), "process": m.group(1) if m else "",
                     "pid": int(m.group(2)) if m else None})
    df = pd.DataFrame(rows, columns=["proto", "address", "port", "exposure", "process", "pid"])
    df["pid"] = df["pid"].astype("Int64")             # whole numbers, with <NA> where the owner is hidden
    return df

def main(argv=None):
    ap = argparse.ArgumentParser(prog="listeners", description="List listening TCP/UDP sockets and how exposed they are.",
                                 epilog="exit status: 0 normally; 1 with --fail-exposed if anything listens beyond loopback")
    ap.add_argument("--proto", choices=("tcp", "udp", "all"), default="all", help="which protocol (default all)")
    ap.add_argument("--exposed", action="store_true", help="only sockets reachable from other machines")
    ap.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    ap.add_argument("--fail-exposed", action="store_true", help="exit 1 if any exposed socket is found (for monitoring)")
    a = ap.parse_args(argv)

    df = listeners()
    if a.proto != "all": df = df[df.proto == a.proto]
    if a.exposed: df = df[df.exposure != "loopback"]
    df = df.sort_values(["exposure", "port"], key=lambda s: s.map({"ALL": 0, "address": 1, "loopback": 2}) if s.name == "exposure" else s)
    if a.csv:
        print(df.to_csv(index=False), end="")
    else:
        console = Console()
        t = Table()
        for c in ("proto", "address", "port", "exposure", "process", "pid"): t.add_column(c)
        for r in df.itertuples():
            t.add_row(r.proto, safe(r.address), str(r.port), r.exposure, safe(r.process or "-"), "-" if pd.isna(r.pid) else str(int(r.pid)),
                      style="bold yellow" if r.exposure == "ALL" else None)
        console.print(t)
        n_all = int((df.exposure == "ALL").sum())
        console.print(f"{len(df)} listening sockets, {n_all} on all interfaces")
        if (df.process == "").any() and not df.empty:
            console.print("[dim]some owners are hidden; run as root to see every process[/dim]")
    return 1 if a.fail_exposed and (df.exposure != "loopback").any() else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

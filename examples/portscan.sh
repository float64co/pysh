#!/usr/local/bin/pysh
"""portscan - find open TCP ports, grab banners, and identify services with nmap.

Only scan systems you own or have written permission to test. To keep that honest this script
refuses anything but loopback and private-network addresses unless you pass --i-am-authorized.
By default it scans this machine.
"""
import argparse, concurrent.futures as cf, datetime as dt, ipaddress, re, shutil, socket, sys
import jinja2
from lxml import etree
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

REPORT = jinja2.Template("""\
# Port scan of {{ host }}{% if addr != host %} ({{ addr }}){% endif %}

Scanned {{ nports }} ports on {{ when }}. {{ rows|length }} open.

| port | service | version / banner |
|-----:|---------|------------------|
{% for r in rows -%}
| {{ r.port }} | {{ r.service or "-" }} | {{ (r.detail or "-")|replace("|", "/") }} |
{% endfor %}
""")

def parse_ports(spec):
    ports = set()
    for part in spec.split(","):
        part = part.strip()
        if not part: continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m: raise argparse.ArgumentTypeError(f"bad port spec {part!r} (use e.g. 22,80,8000-8100)")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= lo <= hi <= 65535): raise argparse.ArgumentTypeError(f"port range {part!r} out of 1-65535")
        ports.update(range(lo, hi + 1))
    if not ports: raise argparse.ArgumentTypeError("no ports given")
    return sorted(ports)

def printable(b):
    return "".join(chr(c) if 32 <= c < 127 else " " for c in b).strip()[:80]

def probe(addr, port, timeout):
    """-> (port, banner) if open, else None"""
    try:
        with socket.create_connection((addr, port), timeout=timeout) as s:
            s.settimeout(0.4)
            try: data = s.recv(200)
            except socket.timeout:
                data = b""
                if port in (80, 8000, 8008, 8080, 8888):
                    s.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
                    try: data = s.recv(200)
                    except (socket.timeout, OSError): pass
            except OSError: data = b""
            return port, printable(data)
    except OSError:
        return None

def allowed(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_loopback or ip.is_private or ip.is_link_local

def nmap_services(addr, ports):
    """{port: (service, 'product version')} from `nmap -sV`, or {} if nmap is missing or fails"""
    if not shutil.which("nmap"): return {}
    r = !(nmap -Pn -sV --version-light -oX - -p @(",".join(map(str, ports))) @(addr))
    if not r.out.strip(): return {}
    try: root = etree.fromstring(r.out.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError: return {}
    found = {}
    for p in root.iter("port"):
        svc = p.find("service")
        if svc is None: continue
        detail = " ".join(x for x in (svc.get("product"), svc.get("version"), svc.get("extrainfo")) if x)
        found[int(p.get("portid"))] = (svc.get("name") or "", detail)
    return found

def main(argv=None):
    ap = argparse.ArgumentParser(prog="portscan", description="TCP connect scan with banner grabbing and optional nmap service detection.",
                                 epilog="examples:  portscan   |   portscan 192.168.1.10 -p 1-10000 --report scan.md   |   portscan --no-nmap -p 22,80,443")
    ap.add_argument("host", nargs="?", default="127.0.0.1", help="host or address to scan (default 127.0.0.1)")
    ap.add_argument("-p", "--ports", type=parse_ports, default=parse_ports("1-1024,3306,5432,6379,8000-8100,8888,11434"),
                    help="ports, e.g. 22,80,8000-8100 (default: 1-1024 plus some common ones)")
    ap.add_argument("-t", "--timeout", type=float, default=0.5, help="connect timeout in seconds (default 0.5)")
    ap.add_argument("-j", "--workers", type=int, default=200, help="parallel connections (default 200)")
    ap.add_argument("--no-nmap", action="store_true", help="skip nmap service detection even if nmap is installed")
    ap.add_argument("--report", metavar="FILE", help="write a Markdown report to FILE")
    ap.add_argument("--i-am-authorized", action="store_true", help="allow scanning a public address (you have permission to test it)")
    a = ap.parse_args(argv)

    try: addr = socket.gethostbyname(a.host)
    except OSError as e: sys.exit(f"portscan: cannot resolve {a.host}: {e}")
    if not allowed(addr) and not a.i_am_authorized:
        sys.exit(f"portscan: {a.host} ({addr}) is not a loopback or private address.\n"
                 f"          only scan systems you have permission to test; add --i-am-authorized to proceed")

    console = Console()
    console.print(f"scanning {safe(a.host)} ({addr}), {len(a.ports)} ports …", highlight=False)
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        open_ports = sorted(r for r in pool.map(lambda p: probe(addr, p, a.timeout), a.ports) if r)

    services = {}
    if open_ports and not a.no_nmap:
        services = nmap_services(addr, [p for p, _ in open_ports])
        if not services and shutil.which("nmap"): console.print("[dim]nmap gave no service details[/dim]")
    elif not shutil.which("nmap") and not a.no_nmap:
        console.print("[dim]nmap not installed: showing banners only[/dim]")

    rows = []
    for port, banner in open_ports:
        svc, detail = services.get(port, ("", ""))
        if not svc:
            try: svc = socket.getservbyport(port, "tcp")
            except OSError: svc = ""
        rows.append({"port": port, "service": svc, "detail": detail or banner})
    if not rows:
        console.print("no open ports found")
    else:
        t = Table(title=f"{len(rows)} open", title_justify="left")
        t.add_column("port", justify="right"); t.add_column("service"); t.add_column("version / banner", overflow="fold")
        for r in rows: t.add_row(str(r["port"]), safe(r["service"] or "-"), safe(r["detail"] or "-"))
        console.print(t)
    if a.report:
        try:
            with open(a.report, "w") as f:
                f.write(REPORT.render(host=a.host, addr=addr, nports=len(a.ports), rows=rows, when=dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
        except OSError as e: sys.exit(f"portscan: {a.report}: {e.strerror}")
        console.print(f"[dim]wrote {safe(a.report)}[/dim]")

if __name__ == "__main__":
    main(sys.argv[1:])

#!/usr/local/bin/pysh
"""certcheck - how many days until each TLS certificate expires?

Connects to every host in parallel, reads the certificate it presents, and prints a table sorted by
time remaining. Exits non-zero if any certificate is close to expiry, expired, or unreachable, so it
can sit in cron. `--demo` needs no network: it starts a local TLS server with a short-lived certificate.
"""
import argparse, concurrent.futures as cf, datetime as dt, os, re, socket, ssl, sys, tempfile, threading
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

def name_of(n):
    cn = n.get_attributes_for_oid(NameOID.COMMON_NAME)
    return cn[0].value if cn else n.rfc4514_string()

def inspect_cert(der):
    c = x509.load_der_x509_certificate(der)
    end = c.not_valid_after_utc if hasattr(c, "not_valid_after_utc") else c.not_valid_after.replace(tzinfo=dt.timezone.utc)
    return name_of(c.subject), name_of(c.issuer), end

def fetch(host, port, timeout):
    """-> dict. Tries a verified connection first, then an unverified one so expired or self-signed certs can still be read."""
    row = {"host": f"{host}:{port}", "trusted": "yes", "error": ""}
    for verify in (True, False):
        ctx = ssl.create_default_context()
        if not verify: ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        try:
            with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as t:
                subject, issuer, end = inspect_cert(t.getpeercert(binary_form=True))
                row.update(subject=subject, issuer=issuer, expires=end,
                           days=(end - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400)
                return row
        except ssl.SSLCertVerificationError as e:
            row["trusted"] = "no"; row["error"] = e.verify_message          # then retry without verification
        except (OSError, ssl.SSLError) as e:
            row["error"] = str(e) or type(e).__name__
            return row
    return row

def demo_server(days):
    """A throw-away TLS server on 127.0.0.1 whose certificate expires in `days` days. -> (port, stop)"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp(prefix="certcheck-")
    pem = os.path.join(tmp, "cert.pem")
    with open(pem, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(pem)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(16); srv.settimeout(0.2)
    done = threading.Event()
    def serve():
        while not done.is_set():
            try: c, _ = srv.accept()
            except socket.timeout: continue
            except OSError: break
            try: ctx.wrap_socket(c, server_side=True).close()
            except (OSError, ssl.SSLError): c.close()
    threading.Thread(target=serve, daemon=True).start()
    def stop():
        done.set(); srv.close()
        for f in os.listdir(tmp): os.unlink(os.path.join(tmp, f))
        os.rmdir(tmp)
    return srv.getsockname()[1], stop

def parse_target(t, default_port):
    host, sep, port = t.rpartition(":")
    return (host, int(port)) if sep and port.isdigit() and host else (t, default_port)

def main(argv=None):
    ap = argparse.ArgumentParser(prog="certcheck", description="Report days until TLS certificates expire.",
                                 epilog="examples:  certcheck example.com github.com:443   |   certcheck -f hosts.txt --warn 30   |   certcheck --demo")
    ap.add_argument("hosts", nargs="*", help="host or host:port")
    ap.add_argument("-f", "--file", help="read more hosts from FILE, one per line ('#' starts a comment)")
    ap.add_argument("-p", "--port", type=int, default=443, help="port when a host doesn't give one (default 443)")
    ap.add_argument("-w", "--warn", type=float, default=14, help="warn when fewer than this many days remain (default 14)")
    ap.add_argument("-t", "--timeout", type=float, default=5, help="connect timeout in seconds (default 5)")
    ap.add_argument("-j", "--workers", type=int, default=8, help="parallel connections (default 8)")
    ap.add_argument("--demo", action="store_true", help="check a local server with 10-day and 400-day certificates (no network)")
    a = ap.parse_args(argv)

    targets = list(a.hosts)
    if a.file:
        try:
            with open(a.file) as f: targets += [l.split("#")[0].strip() for l in f if l.split("#")[0].strip()]
        except OSError as e: sys.exit(f"certcheck: {a.file}: {e.strerror}")
    stops = []
    if a.demo:
        for days in (10, 400):
            port, stop = demo_server(days); stops.append(stop); targets.append(f"127.0.0.1:{port}")
    if not targets:
        ap.print_usage(sys.stderr); print("certcheck: give at least one host (or --demo)", file=sys.stderr); return 2

    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            rows = list(pool.map(lambda t: fetch(*parse_target(t, a.port), a.timeout), targets))
    finally:
        for stop in stops: stop()

    rows.sort(key=lambda r: r.get("days", float("-inf")))      # unreachable first, then soonest to expire
    t = Table()
    for c, j in (("host", "left"), ("days left", "right"), ("expires", "left"), ("subject", "left"), ("trusted", "left"), ("note", "left")):
        t.add_column(c, justify=j, overflow="fold")
    bad = 0
    for r in rows:
        if "days" not in r:
            bad += 1; t.add_row(safe(r["host"]), "-", "-", "-", "-", safe(r["error"]), style="bold red"); continue
        style = "bold red" if r["days"] < 0 else "yellow" if r["days"] < a.warn else None
        bad += style is not None
        note = "EXPIRED" if r["days"] < 0 else ("expires soon" if r["days"] < a.warn else safe(r["error"]))
        t.add_row(safe(r["host"]), f"{r['days']:.1f}", r["expires"].strftime("%Y-%m-%d"), safe(r["subject"]), r["trusted"], note, style=style)
    Console().print(t)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

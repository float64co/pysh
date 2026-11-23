#!/usr/local/bin/pysh
"""ropnotes - a pwntools tour of a binary, for learning and CTF warm-ups.

Inspects an ELF file (it is only ever read, never executed): architecture, hardening flags
(checksec), imported functions, a few useful addresses. Then shows the pwntools building blocks
that exploit scripts use: cyclic patterns and offset finding, and assembling / disassembling code.
"""
import argparse, os, re, sys


def clean(s):
    """text from outside made safe to print: control characters (terminal escapes) become '?', newlines and tabs are kept"""
    return re.sub(r"[\x00-\x08\x0b-\x0c\x0d-\x1f\x7f-\x9f]", "?", str(s))

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ropnotes", description="Inspect an ELF binary and demo pwntools helpers (nothing is executed).",
                                 epilog="examples:  ropnotes   |   ropnotes ./vuln --find 0x6161616b   |   ropnotes ./vuln --gadgets   |   ropnotes --asm 'xor eax, eax; ret'")
    ap.add_argument("binary", nargs="?", default="/bin/ls", help="ELF file to inspect (default /bin/ls)")
    ap.add_argument("--pattern-len", type=int, default=48, help="length of the cyclic pattern to print (default 48)")
    ap.add_argument("--find", metavar="VALUE", help="offset of VALUE (hex like 0x6161616b, or the 4 characters) inside the cyclic pattern")
    ap.add_argument("--asm", metavar="CODE", default="mov rax, 60; xor rdi, rdi; syscall", help="assembly to assemble and disassemble (default: exit(0) on amd64)")
    ap.add_argument("--gadgets", action="store_true", help="search the binary for common ROP gadgets (can take a few seconds)")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.binary): sys.exit(f"ropnotes: {a.binary}: no such file")
    try:
        from pwn import ELF, ROP, asm, context, cyclic, cyclic_find, disasm, shellcraft
    except ImportError:
        sys.exit("ropnotes: pwntools is not installed (pip install pwntools)")
    context.log_level = "error"

    print(f"== {a.binary}")
    print("file(1):", clean($(file -b @(a.binary))))
    try:
        elf = ELF(a.binary, checksec=False)
    except Exception as e:
        sys.exit(f"ropnotes: {a.binary} is not a readable ELF file ({type(e).__name__}: {e})")
    context.binary = elf

    print(f"\n== architecture\n{elf.arch} {elf.bits}-bit {elf.endian} endian, entry point {elf.entry:#x}")
    print("\n== checksec")
    flags = [("RELRO", elf.relro or "None"), ("Stack canary", "yes" if elf.canary else "NO"),
             ("NX (non-executable stack)", "yes" if elf.nx else "NO"), ("PIE (randomised base)", "yes" if elf.pie else "NO"),
             ("RWX segments", "YES" if elf.rwx_segments else "no"), ("Fortify", "yes" if elf.fortify else "no")]
    for name, val in flags: print(f"  {name:<28} {val}")
    if not elf.canary: print("  note: no stack canary, so a stack overflow is not detected before the function returns")

    imports = sorted(set(elf.got) | set(elf.plt))
    risky = [s for s in imports if s in ("gets", "strcpy", "strcat", "sprintf", "system", "popen", "execve", "scanf", "vsprintf", "memcpy")]
    print(f"\n== imports\n{len(imports)} imported functions" + (f"; interesting: {', '.join(risky)}" if risky else ""))
    for name in ("puts", "printf", "system", "read", "write"):
        if name in elf.plt: print(f"  {name}@plt = {elf.plt[name]:#x}")

    if a.gadgets:
        print("\n== gadgets")
        rop = ROP(elf)
        for g in (["pop rdi", "ret"], ["pop rsi", "ret"], ["pop rsi", "pop r15", "ret"], ["ret"], ["syscall"]):
            try: found = rop.find_gadget(g)
            except Exception: found = None
            print(f"  {'; '.join(g):<18} {found.address:#x}" if found else f"  {'; '.join(g):<18} not found")

    print("\n== cyclic pattern (de Bruijn)")
    pat = cyclic(a.pattern_len)
    print(f"  {pat.decode()}")
    if a.find:
        try:
            v = int(a.find, 16) if a.find.lower().startswith("0x") else a.find.encode()
            off = cyclic_find(v)
        except ValueError:
            sys.exit(f"ropnotes: cannot read --find value {a.find!r}")
        print(f"  {a.find} is at offset {off}" if off >= 0 else f"  {a.find} is not in the pattern")
    else:
        sample = int.from_bytes(pat[8:12], "little")
        print(f"  e.g. if a crash shows {sample:#x}, cyclic_find({sample:#x}) = {cyclic_find(sample)}   (try --find {sample:#x})")

    print("\n== assemble / disassemble (amd64)")
    try:
        code = asm(a.asm, arch="amd64", os="linux")
    except Exception as e:
        sys.exit(f"ropnotes: cannot assemble {a.asm!r}: {str(e).strip().splitlines()[-1] if str(e).strip() else type(e).__name__}")
    print(f"  {a.asm}\n  -> {code.hex()}  ({len(code)} bytes)")
    for line in disasm(code, arch="amd64").splitlines(): print("    " + line)
    sh = asm(shellcraft.amd64.linux.sh(), arch="amd64")
    print(f"  shellcraft.sh() assembles to {len(sh)} bytes; null-free: {b'\\x00' not in sh}")

if __name__ == "__main__":
    main(sys.argv[1:])

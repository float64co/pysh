"""A small VT100 emulator + pty session driver for testing pysh's line editor and job control."""
import fcntl, os, pty, re, select, signal, struct, tempfile, termios, time, unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYSH = os.environ.get("PYSH", os.path.join(ROOT, "pysh"))


def cwidth(ch):
    if unicodedata.combining(ch) or ch in "​‌‍️": return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


class Screen:
    """Cursor movement, autowrap (with xterm's pending-wrap state), erase, scroll. SGR is ignored."""
    CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

    def __init__(self, rows=24, cols=80):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.pending = False
        self.scrolled = 0

    def feed(self, text):
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                m = self.CSI.match(text, i)
                if m: self.csi(m.group(1), m.group(2)); i = m.end(); continue
                i += 1; continue
            i += 1
            if ch == "\r": self.c = 0; self.pending = False
            elif ch == "\n": self.linefeed(); self.pending = False
            elif ch == "\b": self.c = max(0, self.c - 1); self.pending = False
            elif ch == "\t": self.c = min(self.cols - 1, (self.c // 8 + 1) * 8)
            elif ch < " ": pass
            else: self.put(ch)

    def put(self, ch):
        w = cwidth(ch)
        if w == 0: return
        if self.pending or self.c + w > self.cols:
            self.c = 0; self.linefeed(); self.pending = False
        self.grid[self.r][self.c] = ch
        if w == 2 and self.c + 1 < self.cols: self.grid[self.r][self.c + 1] = ""
        if self.c + w >= self.cols: self.c = self.cols - 1; self.pending = True
        else: self.c += w

    def linefeed(self):
        if self.r == self.rows - 1:
            self.grid.pop(0); self.grid.append([" "] * self.cols); self.scrolled += 1
        else: self.r += 1

    def csi(self, args, cmd):
        n = [int(x) if x.isdigit() else 0 for x in args.lstrip("?").split(";")] if args else [0]
        a = n[0] or 1
        if cmd == "A": self.r = max(0, self.r - a)
        elif cmd == "B": self.r = min(self.rows - 1, self.r + a)
        elif cmd == "C": self.c = min(self.cols - 1, self.c + a)
        elif cmd == "D": self.c = max(0, self.c - a)
        elif cmd == "H":
            self.r = min(self.rows - 1, max(0, (n[0] or 1) - 1))
            self.c = min(self.cols - 1, max(0, ((n[1] if len(n) > 1 else 0) or 1) - 1))
        elif cmd == "J":
            mode = n[0]
            if mode == 0:
                self.grid[self.r][self.c:] = [" "] * (self.cols - self.c)
                for r in range(self.r + 1, self.rows): self.grid[r] = [" "] * self.cols
            elif mode == 2: self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif cmd == "K":
            if n[0] == 0: self.grid[self.r][self.c:] = [" "] * (self.cols - self.c)
            elif n[0] == 2: self.grid[self.r] = [" "] * self.cols
        if cmd in "ABCDH": self.pending = False   # explicit cursor motion cancels the pending wrap

    def resize(self, rows, cols):
        """Like xterm: no reflow; rows are cut or padded and the cursor stays where it was (clamped)."""
        grid = [[" "] * cols for _ in range(rows)]
        for r in range(min(rows, self.rows)):
            for c in range(min(cols, self.cols)): grid[r][c] = self.grid[r][c]
        self.grid, self.rows, self.cols = grid, rows, cols
        self.r, self.c, self.pending = min(self.r, rows - 1), min(self.c, cols - 1), False

    def lines(self):
        return ["".join(row).rstrip() for row in self.grid]

    def text(self):
        ls = self.lines()
        while ls and not ls[-1]: ls.pop()
        return "\n".join(ls)


class Session:
    """pysh running on a pty, with its output fed into a Screen."""

    def __init__(self, args=(), rows=24, cols=80, home=None, history=None, env=None, binary=None, answer_dsr=True):
        self.answer_dsr = answer_dsr
        self.home = home or tempfile.mkdtemp(prefix="pysh-test-")
        if history is not None:
            with open(os.path.join(self.home, ".pysh_history"), "w") as f: f.write("".join(h + "\n" for h in history))
        self.screen = Screen(rows, cols)
        self.raw = b""
        e = dict(os.environ, HOME=self.home, TERM="xterm", USER="tester", PYSH_HISTFILE=os.path.join(self.home, ".pysh_history"))
        e.pop("PROMPT_COMMAND", None)
        e.update(env or {})
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(self.home)
            os.execve(binary or PYSH, ["pysh", *args], e)
        self.wait_for(">", 8.0)
        self.pump(0.1)

    def pump(self, quiet=0.25, limit=5.0):
        """Read until the output has been quiet for `quiet` seconds."""
        end = time.time() + limit
        while time.time() < end:
            if not select.select([self.fd], [], [], quiet)[0]: break
            try: d = os.read(self.fd, 65536)
            except OSError: break
            if not d: break
            self.raw += d
            self.screen.feed(d.decode("utf-8", "replace"))
            if self.answer_dsr and d.endswith(b"\x1b[6n"):            # cursor position report
                os.write(self.fd, f"\x1b[{self.screen.r + 1};{self.screen.c + 1}R".encode())

    def send(self, data, quiet=0.25):
        if isinstance(data, str): data = data.encode()
        os.write(self.fd, data)
        self.pump(quiet)
        return self.screen.text()

    def wait_for(self, text, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.screen.text(): return True
            self.pump(0.1, 0.5)
        return text in self.screen.text()

    def text(self): return self.screen.text()

    def resize(self, rows, cols, quiet=0.5):
        """Resize the pty (the kernel sends SIGWINCH to pysh) and the emulated screen."""
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.screen.resize(rows, cols)
        self.pump(quiet)

    def close(self):
        try: os.close(self.fd)
        except OSError: pass
        try: os.kill(self.pid, signal.SIGKILL)
        except OSError: pass
        try: os.waitpid(self.pid, 0)
        except OSError: pass

    def wait_exit(self, timeout=5.0):
        """True once the process has exited (polls; exits can lag under load)."""
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive(): return True
            self.pump(0.1, 0.3)
        return not self.alive()

    def alive(self):
        try: return os.waitpid(self.pid, os.WNOHANG) == (0, 0)
        except ChildProcessError: return False

/* pysh: a shell whose "other half" is an embedded CPython.
 *
 * C side: interpreter bootstrap + a tiny raw-mode line editor (_pyshline),
 * needed because the static binary cannot load readline.so / termios.so.
 * Everything else lives in pysh.py (embedded via pysh_py.h). */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>
#include <fcntl.h>
#include <locale.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>
#include <sys/ioctl.h>

#include "pysh_py.h"

#ifndef PYSH_HOME
#define PYSH_HOME "/usr"
#endif

/* ---------------------------------------------------------------- editor */

#define MAXLINE (256 * 1024)

static volatile sig_atomic_t g_winch;     /* set by SIGWINCH while a line is being edited */
static void on_winch(int sig) { (void)sig; g_winch = 1; }
static int pushback = -1;                 /* a key read ahead while coalescing typed/pasted text */

/* Next byte of input.  Returns -1 on timeout, -2 on EOF/error, and -3 (blocking reads only) when the
 * terminal was resized, so the editor can repaint. */
static int key(int timeout_ms) {
    unsigned char c;
    if (pushback >= 0) { c = (unsigned char)pushback; pushback = -1; return c; }
    if (timeout_ms >= 0) {
        struct pollfd p = {0, POLLIN, 0};
        if (poll(&p, 1, timeout_ms) <= 0) return -1;
    }
    for (;;) {
        if (g_winch && timeout_ms < 0) return -3;               /* a resize arrived while we weren't reading */
        ssize_t n = read(0, &c, 1);
        if (n == 1) return c;
        if (n == 0) return -2;
        if (errno == EINTR) { if (g_winch && timeout_ms < 0) return -3; continue; }
        return -2;
    }
}

static int is_cont(unsigned char c) { return (c & 0xC0) == 0x80; }
static int prev_ch(const char *b, int p) { do p--; while (p > 0 && is_cont(b[p])); return p; }
static int next_ch(const char *b, int len, int p) { do p++; while (p < len && is_cont(b[p])); return p; }

static int g_log = -1;     /* PYSH_TTYLOG=file: copy of everything the editor writes, for bug reports */
static void out(const char *s, size_t n) {
    if (g_log >= 0) { ssize_t r = write(g_log, s, n); (void)r; }
    while (n) { ssize_t w = write(1, s, n); if (w <= 0) { if (errno == EINTR) continue; return; } s += w; n -= w; }
}
static void outs(const char *s) { out(s, strlen(s)); }
static void outf(const char *fmt, int n) { char t[32]; snprintf(t, sizeof t, fmt, n); outs(t); }

/* ---- display width (approximate wcwidth) ---- */
static int cpwidth(unsigned cp) {
    if (cp == 0) return 0;
    if ((cp >= 0x300 && cp <= 0x36F) || (cp >= 0x200B && cp <= 0x200F) || (cp >= 0xFE00 && cp <= 0xFE0F)) return 0;
    if ((cp >= 0x1100 && cp <= 0x115F) || (cp >= 0x2E80 && cp <= 0xA4CF) || (cp >= 0xAC00 && cp <= 0xD7A3) ||
        (cp >= 0xF900 && cp <= 0xFAFF) || (cp >= 0xFE30 && cp <= 0xFE6F) || (cp >= 0xFF00 && cp <= 0xFF60) ||
        (cp >= 0xFFE0 && cp <= 0xFFE6) || (cp >= 0x1F300 && cp <= 0x1F64F) || (cp >= 0x1F900 && cp <= 0x1F9FF) ||
        (cp >= 0x20000 && cp <= 0x3FFFD)) return 2;
    return 1;
}
/* width of the first n bytes of s; skips CSI escape sequences (for prompts / highlighted text) */
static int text_width(const char *s, int n) {
    int w = 0;
    for (int i = 0; i < n;) {
        unsigned char c = s[i];
        if (c == 0x1b && i + 1 < n && s[i+1] == '[') {
            i += 2; while (i < n && !(s[i] >= 0x40 && s[i] <= 0x7e)) i++;
            i++; continue;
        }
        unsigned cp; int l;
        if (c < 0x80) { cp = c; l = 1; }
        else if (c >= 0xF0) { cp = c & 7; l = 4; }
        else if (c >= 0xE0) { cp = c & 15; l = 3; }
        else if (c >= 0xC0) { cp = c & 31; l = 2; }
        else { i++; continue; }
        for (int k = 1; k < l && i + k < n; k++) cp = (cp << 6) | (s[i+k] & 63);
        i += l;
        w += cpwidth(cp);
    }
    return w;
}

static void term_size(int *w, int *h) {
    struct winsize ws;
    *w = 80; *h = 24;
    if (ioctl(1, TIOCGWINSZ, &ws) == 0) { if (ws.ws_col) *w = ws.ws_col; if (ws.ws_row) *h = ws.ws_row; }
}

static PyObject *g_suggest, *g_highlight, *g_search;
static int m_active, m_search;
static char g_sug[MAXLINE];   /* current suggestion tail (only when cursor at end) */
static int g_row;             /* cursor row relative to the first row of the prompt */
static int g_w = 80;          /* terminal width at the last repaint */
static int g_below;           /* rows of menu currently drawn below the line */

/* ------------------------------------------------------ completion menu */
static PyObject *m_items;          /* list of (full_before_cursor, display, desc) */
static Py_ssize_t m_n;
static int m_sel;
static char m_tail[MAXLINE]; static int m_taillen;
static char m_orig[MAXLINE]; static int m_origlen, m_origpos;

/* display width of one UTF-8 sequence starting at p (advances *n bytes); control chars count as 1 ('?') */
static int char_w(const char *p, int *n) {
    unsigned char c = p[0];
    int l = c < 0x80 ? 1 : c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : c >= 0xC0 ? 2 : 1;
    for (int k = 1; k < l; k++) if (!is_cont((unsigned char)p[k])) { l = k; break; }
    *n = l;
    if (c < 0x20 || c == 0x7f) return 1;
    return text_width(p, l);
}
static int str_w(const char *s) {
    int w = 0, n;
    while (*s) { w += char_w(s, &n); s += n; }
    return w;
}

static void item(Py_ssize_t i, const char **full, const char **disp, const char **desc) {
    PyObject *t = PyList_GetItem(m_items, i);
    *full = PyUnicode_AsUTF8(PyTuple_GetItem(t, 0));
    *disp = PyUnicode_AsUTF8(PyTuple_GetItem(t, 1));
    *desc = PyUnicode_AsUTF8(PyTuple_GetItem(t, 2));
    if (!*full) { PyErr_Clear(); *full = ""; }
    if (!*disp) { PyErr_Clear(); *disp = ""; }
    if (!*desc) { PyErr_Clear(); *desc = ""; }
}
static void pad(int n) { while (n-- > 0) outs(" "); }

/* print s clipped to `room` columns (control characters shown as '?'); returns the columns used */
static int out_w(const char *s, int room) {
    int used = 0, n;
    while (*s) {
        int w = char_w(s, &n);
        if (used + w > room) break;
        unsigned char c = s[0];
        if (c < 0x20 || c == 0x7f) outs("?"); else out(s, n);
        used += w; s += n;
    }
    return used;
}

/* Draw the menu below the line (cursor is on the line's last row). Returns the rows drawn;
 * the cursor is left on the last menu row. */
static int menu_draw(int tw, int th, int endrow) {
    int colw = 0;
    for (Py_ssize_t i = 0; i < m_n; i++) {
        const char *f, *d, *e; item(i, &f, &d, &e);
        int w = str_w(d) + (*e ? 2 + str_w(e) : 0);
        if (w > colw) colw = w;
    }
    if (colw > tw - 3) colw = tw - 3;
    int cols = tw / (colw + 2); if (cols < 1 || m_search) cols = 1;
    int rows = (int)((m_n + cols - 1) / cols);
    int maxrows = th - endrow - 2; if (maxrows < 1) maxrows = 1;
    int first = 0, shown = rows;
    if (rows > maxrows) {
        shown = maxrows;
        int selrow = m_sel >= 0 ? m_sel / cols : 0;
        if (selrow >= maxrows) first = selrow - maxrows + 1;
    }
    for (int r = first; r < first + shown; r++) {
        outs("\r\n");
        for (int c = 0; c < cols; c++) {
            Py_ssize_t i = (Py_ssize_t)r * cols + c;
            if (i >= m_n) break;
            const char *f, *d, *e; item(i, &f, &d, &e);
            int used;
            if (i == m_sel) outs("\x1b[7m");
            used = out_w(d, colw);
            if (*e && colw - used > 3) {
                outs(i == m_sel ? "  " : "  \x1b[90m");
                used += 2 + out_w(e, colw - used - 2);
            }
            outs("\x1b[0m");
            pad(colw - used + 2);
        }
    }
    return shown;
}

static void menu_close(void) {
    m_active = 0; m_search = 0;
    Py_CLEAR(m_items);
}

/* ------------------------------------------------------------- redraw */
/* Repaint prompt + text (+ suggestion) + menu from the first row of the prompt, then park the
 * cursor at byte offset `pos`.  Wrapping is handled by tracking the terminal width. */
static void redraw(const char *prompt, int pw, const char *buf, int len, int pos) {
    int W, H;
    term_size(&W, &H);
    g_w = W;
    g_sug[0] = 0;
    if (pos == len && len > 0 && g_suggest != Py_None && !m_active) {
        PyObject *r = PyObject_CallFunction(g_suggest, "s#", buf, (Py_ssize_t)len);
        if (r) {
            const char *t = PyUnicode_Check(r) ? PyUnicode_AsUTF8(r) : NULL;
            if (t && strlen(t) < MAXLINE) strcpy(g_sug, t);
            Py_DECREF(r);
        } else PyErr_Clear();
    }
    if (g_row > 0) outf("\x1b[%dA", g_row);
    outs("\r"); outs(prompt);
    PyObject *h = g_highlight != Py_None ? PyObject_CallFunction(g_highlight, "s#", buf, (Py_ssize_t)len) : NULL;
    const char *hs = h && PyUnicode_Check(h) ? PyUnicode_AsUTF8(h) : NULL;
    if (hs) outs(hs); else { PyErr_Clear(); out(buf, len); }
    Py_XDECREF(h);
    if (g_sug[0]) { outs("\x1b[90m"); outs(g_sug); outs("\x1b[0m"); }
    int tw = pw + text_width(buf, len) + (g_sug[0] ? text_width(g_sug, (int)strlen(g_sug)) : 0);
    if (tw > 0 && tw % W == 0) outs(" \r");        /* force the pending wrap so rows are predictable */
    int endrow = tw / W;
    outs("\x1b[J");
    if (g_below > 0 && g_below < H) {                 /* belt and braces: blank each old menu row explicitly */
        for (int k = 0; k < g_below; k++) outs("\x1b[1B\r\x1b[2K");
        outf("\x1b[%dA", g_below);
    }
    g_below = 0;
    int bottom = endrow;
    if (m_active && m_n > 0 && m_items) { g_below = menu_draw(W, H, endrow); bottom += g_below; }
    int tc = pw + text_width(buf, pos), tr = tc / W, tcol = tc % W;
    if (bottom > tr) outf("\x1b[%dA", bottom - tr);
    else if (bottom < tr) outf("\x1b[%dB", tr - bottom);
    outs("\r");
    if (tcol) outf("\x1b[%dC", tcol);
    g_row = tr;
}

static void ins(char *buf, int *len, int *pos, const char *s, int n) {
    if (*len + n >= MAXLINE) { outs("\a"); return; }           /* line full: beep instead of silently dropping */
    memmove(buf + *pos + n, buf + *pos, *len - *pos);
    memcpy(buf + *pos, s, n);
    *len += n; *pos += n;
}
static void del(char *buf, int *len, int from, int to, int *pos) {
    memmove(buf + from, buf + to, *len - to);
    *len -= to - from;
    if (*pos > to) *pos -= to - from; else if (*pos > from) *pos = from;
}
static int word_back(const char *b, int p) {
    while (p > 0 && b[p-1] == ' ') p--;
    while (p > 0 && b[p-1] != ' ') p--;
    return p;
}
static int word_fwd(const char *b, int len, int p) {
    while (p < len && b[p] == ' ') p++;
    while (p < len && b[p] != ' ') p++;
    return p;
}

/* insert the character whose first byte is c (reading the rest of a UTF-8 sequence) */
static void ins_key(char *buf, int *len, int *pos, int c) {
    char ch[4]; int n = 1; ch[0] = (char)c;
    if (c >= 0xC0) {
        int more = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : 1;
        for (int i = 0; i < more; i++) { int x = key(-1); if (x < 0) break; ch[n++] = (char)x; }
    }
    ins(buf, len, pos, ch, n);
}

/* Ask the terminal where the cursor is (1-based).  Returns 0 if it doesn't answer within 200 ms. */
static int cursor_pos(int *row, int *col) {
    char b[48]; int n = 0;
    pushback = -1;
    outs("\x1b[6n");
    for (;;) {
        int c = key(200);
        if (c < 0) return 0;
        if (n < (int)sizeof b - 1) b[n++] = (char)c;
        if (c == 'R') break;
    }
    b[n] = 0;
    char *p = strstr(b, "\x1b[");
    return p && sscanf(p + 2, "%d;%d", row, col) == 2;
}

/* Cursor position and the terminal's real width (it can differ from the pty's size for a moment while a
 * multiplexer or window is still being resized).  The cursor is put back where it was.  Returns 1 only if the
 * cursor reads the same before and after the width probe, i.e. the terminal was not changing underneath us. */
static int probe_terminal(int *row, int *col, int *width) {
    int r2 = 0, c2 = 0, r3 = 0, c3 = 0;
    if (!cursor_pos(row, col)) return -1;
    outs("\x1b[999C");
    *width = cursor_pos(&r2, &c2) ? c2 : 0;
    outs("\r");
    if (*col > 1) outf("\x1b[%dC", *col - 1);
    if (!cursor_pos(&r3, &c3)) return -1;
    return r3 == *row && c3 == *col;
}

/* Terminals that reflow wrapped lines when resized: the old text is re-laid out at the new width, so we
 * only need the cursor's new row.  Others keep the old rows, so start a fresh prompt below them.
 * PYSH_RESIZE=reflow|redraw overrides the guess. */
static int terminal_reflows(void) {
    const char *e = getenv("PYSH_RESIZE");
    if (e) return strcmp(e, "reflow") == 0;
    return getenv("WT_SESSION") || getenv("VTE_VERSION") || getenv("KITTY_WINDOW_ID") || getenv("ITERM_SESSION_ID") ||
           getenv("WEZTERM_PANE") || getenv("TMUX");
}

/* Ctrl-R: re-run the history query for what's in the buffer */
static void search_refresh(void *unused, const char *buf, int len) {
    (void)unused;
    Py_CLEAR(m_items);
    PyObject *r = PyObject_CallFunction(g_search, "s#", buf, (Py_ssize_t)len);
    if (!r || !PyList_Check(r)) { PyErr_Clear(); Py_XDECREF(r); r = PyList_New(0); }
    m_items = r; m_n = PyList_Size(r);
    m_sel = m_n ? 0 : -1;
}

/* readline(prompt, history, completer, suggest, highlight, search) -> str */
static PyObject *le_readline(PyObject *self, PyObject *args) {
    const char *prompt; PyObject *hist, *comp;
    if (!PyArg_ParseTuple(args, "sOOOOO", &prompt, &hist, &comp, &g_suggest, &g_highlight, &g_search)) return NULL;

    struct termios old, raw;
    if (tcgetattr(0, &old) < 0) { PyErr_SetFromErrno(PyExc_OSError); return NULL; }
    raw = old;
    raw.c_lflag &= ~(ICANON | ECHO | ISIG | IEXTEN);
    raw.c_iflag &= ~(IXON | ICRNL | INLCR);
    raw.c_cc[VMIN] = 1; raw.c_cc[VTIME] = 0;
    tcsetattr(0, TCSADRAIN, &raw);
    struct sigaction sa, old_sa;
    memset(&sa, 0, sizeof sa); sa.sa_handler = on_winch; sigemptyset(&sa.sa_mask);   /* no SA_RESTART: read() must wake */
    sigaction(SIGWINCH, &sa, &old_sa);
    g_winch = 0; pushback = -1;
    int reflow = terminal_reflows();
    {   /* Safety net: if a child left itself as the terminal's foreground group (e.g. it died before exec), take it
         * back; otherwise our reads fail with EIO and the shell would think its input has ended. */
        struct sigaction ttou;
        if (sigaction(SIGTTOU, NULL, &ttou) == 0 && ttou.sa_handler == SIG_IGN) {
            pid_t fg = tcgetpgrp(0);
            if (fg > 0 && fg != getpgrp()) tcsetpgrp(0, getpgrp());
        }
    }

    static char buf[MAXLINE], saved[MAXLINE];
    int len = 0, pos = 0;
    Py_ssize_t nh = PyList_Size(hist), hi = nh;
    PyObject *result = NULL;
    int saved_len = 0;
    int pw = text_width(prompt, (int)strlen(prompt));
    buf[0] = 0;
    m_active = m_search = 0; Py_CLEAR(m_items); g_row = 0; g_below = 0;
    if (g_log < 0 && getenv("PYSH_TTYLOG")) g_log = open(getenv("PYSH_TTYLOG"), O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (g_log >= 0) { int w, h; char t[64]; term_size(&w, &h); int n = snprintf(t, sizeof t, "\n<<readline %dx%d>>\n", w, h); ssize_t r = write(g_log, t, n); (void)r; }
    {   /* zsh-style "prompt space": if the previous output left the cursor mid-line, mark it with a
         * reverse-video % and start the prompt on a fresh row; at column 0 this is invisible */
        int w, h; term_size(&w, &h);
        outs("\x1b[7m%\x1b[0m");
        for (int i = 1; i < w; i++) outs(" ");
        outs("\r\x1b[K");
    }
    redraw(prompt, pw, buf, len, pos);

#define REDRAW() redraw(prompt, pw, buf, len, pos)
    for (;;) {
        int c = key(-1);
        if (c == -3) {                                     /* the terminal was resized */
            int W, H, row = 0, col = 0, tw = 0, have, oldW = g_w, mode;       /* mode: 0 reflowed, 1 unchanged, 2 unknown */
            for (int tries = 0;; tries++) {
                do { g_winch = 0; poll(NULL, 0, 80); } while (g_winch);       /* a drag sends a burst: let it settle */
                term_size(&W, &H);
                have = probe_terminal(&row, &col, &tw);
                if (have == 0 && tries < 8) continue;                         /* still changing: look again */
                if (have == 1 && tw && tw != W && tries < 8) continue;        /* terminal and pty disagree: the pty will catch up */
                break;
            }
            int tc = pw + text_width(buf, pos), new_row = tc / W;
            if (have == 1) {
                int ok_new = (col - 1 == tc % W);                            /* where the cursor would be if the text was re-wrapped */
                int ok_old = (col - 1 == tc % oldW) && row - g_row >= 1;     /* ... or if nothing moved */
                mode = ok_new && ok_old ? (reflow ? 0 : 1) : ok_new ? 0 : ok_old ? 1 : 2;
            } else mode = reflow ? 0 : 2;                                           /* no (reliable) answer: use the hint */
            if (g_log >= 0) { char t[160]; int n = snprintf(t, sizeof t, "\n<<resize %d -> %d cursor=%d;%d tc=%d g_row=%d mode=%d>>\n", oldW, W, row, col, tc, g_row, mode); ssize_t r = write(g_log, t, n); (void)r; }
            g_below = 0;
            if (mode == 0 && new_row > row - 1) {
                /* The top of the line has scrolled into the history.  Wipe the visible remainder and start below
                 * a blank row, so the history's wrapped fragment can't be joined to the new prompt on a later resize. */
                if (row > 1) outf("\x1b[%dA", row - 1);
                outs("\r\x1b[J\r\n");
                g_row = 0;
            } else if (mode == 0) g_row = new_row;
            else if (mode == 2) { outs("\r\n"); g_row = 0; }
            REDRAW();
            continue;
        }
        if (c == -2) { PyErr_SetString(PyExc_EOFError, "eof"); break; }
        int changed = 1, refresh = 1;
        if (m_active && m_search && (c == '\r' || c == '\n')) {      /* take the chosen history entry */
            if (m_sel >= 0) {
                const char *f, *d, *e; item(m_sel, &f, &d, &e);
                int n = (int)strnlen(f, MAXLINE - 1); memcpy(buf, f, n); len = pos = n;
            }
            menu_close(); REDRAW(); continue;
        }
        if (m_active && m_search && c == 3) { menu_close(); REDRAW(); }
        if (m_active && !m_search && c != 9 && c != 27 && !((c == '\r' || c == '\n') && m_sel >= 0)) {
            menu_close(); REDRAW();
        }
        if (m_active && !m_search && (c == '\r' || c == '\n'))            /* selection is already in the buffer: close the menu and run it */
            menu_close(), REDRAW();
        switch (c) {
        case '\r': case '\n':
            { PyObject *sv = g_suggest; g_suggest = Py_None; pos = len; REDRAW(); g_suggest = sv; }
            outs("\r\n");
            result = PyUnicode_DecodeUTF8(buf, len, "replace");
            goto done;
        case 3: /* ^C */
            pos = len; { PyObject *sv = g_suggest; g_suggest = Py_None; REDRAW(); g_suggest = sv; }
            outs("^C\r\n");
            PyErr_SetNone(PyExc_KeyboardInterrupt);
            goto done;
        case 4: /* ^D */
            if (len == 0) { outs("\r\n"); PyErr_SetString(PyExc_EOFError, "eof"); goto done; }
            if (pos < len) del(buf, &len, pos, next_ch(buf, len, pos), &pos);
            break;
        case 1: pos = 0; break;
        case 5: if (pos == len && g_sug[0]) ins(buf, &len, &pos, g_sug, strlen(g_sug)); else pos = len; break;
        case 2: if (pos > 0) pos = prev_ch(buf, pos); break;
        case 6: if (pos == len && g_sug[0]) ins(buf, &len, &pos, g_sug, strlen(g_sug)); else if (pos < len) pos = next_ch(buf, len, pos); break;
        case 11: len = pos; break;                       /* ^K */
        case 21: del(buf, &len, 0, pos, &pos); break;    /* ^U */
        case 23: del(buf, &len, word_back(buf, pos), pos, &pos); break; /* ^W */
        case 12: outs("\x1b[H\x1b[2J"); g_row = 0; break;  /* ^L */
        case 127: case 8:
            if (pos > 0) del(buf, &len, prev_ch(buf, pos), pos, &pos);
            break;
        case 20: /* ^T transpose */
            changed = 0; break;
        case 16: case 14: { /* ^P ^N */
            goto hist_nav_dispatch;
        }
        case 18: /* ^R: history search menu */
            if (m_active && m_search) { m_sel = m_n ? (int)((m_sel + 1) % m_n) : -1; refresh = 0; goto redraw_now; }
            if (g_search == Py_None) { changed = 0; break; }
            memcpy(m_orig, buf, len); m_origlen = len; m_origpos = pos; m_taillen = 0;
            m_active = 1; m_search = 1; m_n = 0;
            break;
        case 9: { /* TAB: complete / cycle the menu */
            if (m_active) { if (m_n) m_sel = (int)((m_sel + 1) % m_n); goto menu_select; }
            PyObject *r = PyObject_CallFunction(comp, "s#", buf, (Py_ssize_t)pos);
            if (!r) { PyErr_Print(); changed = 0; break; }
            const char *rep; PyObject *lst;
            if (PyArg_ParseTuple(r, "sO", &rep, &lst) && PyList_Check(lst) && PyList_Size(lst) > 0) {
                int n = strlen(rep);
                m_taillen = len - pos; memcpy(m_tail, buf + pos, m_taillen);
                memcpy(m_orig, buf, len); m_origlen = len; m_origpos = pos;
                if (PyList_Size(lst) == 1) {
                    const char *f = PyUnicode_AsUTF8(PyTuple_GetItem(PyList_GetItem(lst, 0), 0));
                    if (f) rep = f, n = strlen(f);
                } else {
                    Py_INCREF(lst); m_items = lst; m_n = PyList_Size(lst); m_sel = -1; m_active = 1;
                }
                if (n + m_taillen < MAXLINE) { memcpy(buf, rep, n); memcpy(buf + n, m_tail, m_taillen); len = n + m_taillen; pos = n; }
            } else PyErr_Clear();
            Py_DECREF(r);
            break;
        }
        case 27: {
            int c1 = key(50);
            if (m_active && c1 == -1) {                       /* bare ESC cancels */
                menu_close(); memcpy(buf, m_orig, m_origlen); len = m_origlen; pos = m_origpos;
                break;
            }
            if (c1 == 'b') { pos = word_back(buf, pos); break; }
            if (c1 == 'f') { if (pos == len && g_sug[0]) ins(buf, &len, &pos, g_sug, word_fwd(g_sug, strlen(g_sug), 0)); else pos = word_fwd(buf, len, pos); break; }
            if (c1 == 'd') { del(buf, &len, pos, word_fwd(buf, len, pos), &pos); break; }
            if (c1 == 127) { del(buf, &len, word_back(buf, pos), pos, &pos); break; }
            if (c1 != '[' && c1 != 'O') { changed = 0; break; }
            int c2 = key(50), arg = 0;
            while (c2 >= '0' && c2 <= '9') { arg = arg * 10 + (c2 - '0'); c2 = key(50); }
            int mod = 0;
            if (c2 == ';') { mod = key(50) - '0'; c2 = key(50); }
            if (m_active && (c2 == 'A' || c2 == 'B' || c2 == 'Z' || (!m_search && (c2 == 'C' || c2 == 'D')))) {
                if (m_n == 0) { changed = 0; break; }
                if (m_sel < 0) m_sel = (c2 == 'D' || c2 == 'A' || c2 == 'Z') ? (int)m_n - 1 : 0;
                else if (c2 == 'C' || c2 == 'B') m_sel = (int)((m_sel + 1) % m_n);
                else m_sel = (int)((m_sel + m_n - 1) % m_n);
                goto menu_select;
            }
            if (m_active && !m_search) { menu_close(); REDRAW(); }
            switch (c2) {
            case 'A': c = 16; goto hist_nav_dispatch;
            case 'B': c = 14; goto hist_nav_dispatch;
            case 'C':
                if (pos == len && g_sug[0]) {
                    int n = mod >= 5 ? (int)(word_fwd(g_sug, strlen(g_sug), 0)) : (int)strlen(g_sug);
                    ins(buf, &len, &pos, g_sug, n);
                } else if (mod >= 5) pos = word_fwd(buf, len, pos); else if (pos < len) pos = next_ch(buf, len, pos);
                break;
            case 'D': if (mod >= 5) pos = word_back(buf, pos); else if (pos > 0) pos = prev_ch(buf, pos); break;
            case 'H': pos = 0; break;
            case 'F': if (pos == len && g_sug[0]) ins(buf, &len, &pos, g_sug, strlen(g_sug)); else pos = len; break;
            case '~':
                if (arg == 3 && pos < len) del(buf, &len, pos, next_ch(buf, len, pos), &pos);
                else if (arg == 1 || arg == 7) pos = 0;
                else if (arg == 4 || arg == 8) pos = len;
                break;
            default: changed = 0;
            }
            break;
        }
        default:
            if (c >= 32 || c < 0) {
                ins_key(buf, &len, &pos, c);
                for (;;) {                                  /* pasted/typed-ahead text: insert it all, repaint once */
                    int x = key(0);
                    if (x < 0) break;
                    if (x < 32 || x == 127 || x == 0x1b) { pushback = x; break; }
                    ins_key(buf, &len, &pos, x);
                }
            } else changed = 0;
        }
        goto redraw_now;

    hist_nav_dispatch:
        {
            /* fish-style: up/down search history entries that start with what was typed */
            if (hi == nh) { memcpy(saved, buf, len); saved_len = len; }
            Py_ssize_t j = hi; int dir = c == 16 ? -1 : 1;
            for (;;) {
                j += dir;
                if (j < 0) { j = hi; break; }
                if (j >= nh) { j = nh; break; }
                const char *s = PyUnicode_AsUTF8(PyList_GetItem(hist, j));
                if (s && strncmp(s, saved, saved_len) == 0) break;
            }
            hi = j;
            if (hi == nh) { memcpy(buf, saved, saved_len); len = pos = saved_len; }
            else { const char *s = PyUnicode_AsUTF8(PyList_GetItem(hist, hi)); if (s) { len = pos = (int)strnlen(s, MAXLINE - 1); memcpy(buf, s, len); } }
        }
        goto redraw_now;
    menu_select:
        refresh = 0;
        if (m_n > 0 && m_sel >= 0 && !m_search) {
            const char *f, *d, *e; item(m_sel, &f, &d, &e);
            int n = strlen(f);
            if (n + m_taillen < MAXLINE) { memcpy(buf, f, n); memcpy(buf + n, m_tail, m_taillen); len = n + m_taillen; pos = n; }
        }
    redraw_now:
        if (changed) {
            if (m_active && m_search && refresh) search_refresh(NULL, buf, len);
            REDRAW();
        }
    }
done:
    sigaction(SIGWINCH, &old_sa, NULL);
    m_active = m_search = 0; Py_CLEAR(m_items);
    tcsetattr(0, TCSADRAIN, &old);
    return result;
}

static PyObject *le_getmode(PyObject *self, PyObject *args) {
    struct termios t;
    if (tcgetattr(0, &t) < 0) return PyErr_SetFromErrno(PyExc_OSError);
    return PyBytes_FromStringAndSize((const char *)&t, sizeof t);
}
static PyObject *le_setmode(PyObject *self, PyObject *args) {
    Py_buffer b;
    if (!PyArg_ParseTuple(args, "y*", &b)) return NULL;
    if (b.len != sizeof(struct termios)) { PyBuffer_Release(&b); PyErr_SetString(PyExc_ValueError, "bad mode"); return NULL; }
    int r = tcsetattr(0, TCSADRAIN, (const struct termios *)b.buf);
    PyBuffer_Release(&b);
    if (r < 0) return PyErr_SetFromErrno(PyExc_OSError);
    Py_RETURN_NONE;
}

static PyMethodDef le_methods[] = {
    {"readline", le_readline, METH_VARARGS, "readline(prompt, history, completer, suggest, highlight, search)"},
    {"getmode", le_getmode, METH_NOARGS, "terminal attributes of fd 0 as bytes"},
    {"setmode", le_setmode, METH_VARARGS, "restore terminal attributes from getmode()"},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef le_mod = {PyModuleDef_HEAD_INIT, "_pyshline", NULL, -1, le_methods};
static PyObject *PyInit__pyshline(void) { return PyModule_Create(&le_mod); }

/* ------------------------------------------------------------------ main */

int main(int argc, char **argv) {
    setlocale(LC_CTYPE, "");
    if (PyImport_AppendInittab("_pyshline", PyInit__pyshline) < 0) return 1;

    PyConfig cfg;
    PyConfig_InitPythonConfig(&cfg);
    cfg.parse_argv = 0;          /* pysh does its own argument handling */
    cfg.install_signal_handlers = 1;

    PyStatus st;
    /* A static binary has no landmark files next to it; point at the same
     * prefix the system python uses so stdlib + site-packages resolve
     * identically.  PYTHONHOME still overrides. */
    if (!getenv("PYTHONHOME")) {
        const char *h = getenv("PYSH_HOME");
        st = PyConfig_SetBytesString(&cfg, &cfg.home, h ? h : PYSH_HOME);
        if (PyStatus_Exception(st)) goto fail;
    }
    st = PyConfig_SetBytesString(&cfg, &cfg.program_name, "pysh");
    if (PyStatus_Exception(st)) goto fail;
    st = PyConfig_SetBytesArgv(&cfg, argc, argv);
    if (PyStatus_Exception(st)) goto fail;
    st = Py_InitializeFromConfig(&cfg);
    if (PyStatus_Exception(st)) goto fail;
    PyConfig_Clear(&cfg);

    PyObject *m = PyImport_AddModule("__main__");
    PyObject *d = PyModule_GetDict(m);
    PyObject *r = PyRun_StringFlags((const char *)pysh_py, Py_file_input, d, d, NULL);
    if (!r) {
        if (PyErr_ExceptionMatches(PyExc_SystemExit)) {
            int code = 0;
            PyObject *t, *v, *tb;
            PyErr_Fetch(&t, &v, &tb);
            if (v) {
                PyObject *c = PyObject_GetAttrString(v, "code");
                if (c && c != Py_None) code = PyLong_Check(c) ? (int)PyLong_AsLong(c) : 1;
                Py_XDECREF(c);
            }
            Py_Finalize();
            return code;
        }
        PyErr_Print();
        Py_Finalize();
        return 1;
    }
    Py_DECREF(r);
    return Py_FinalizeEx() < 0 ? 120 : 0;

fail:
    PyConfig_Clear(&cfg);
    Py_ExitStatusException(st);
    return 1;
}

/* pysh: a shell whose "other half" is an embedded CPython.
 *
 * C side: interpreter bootstrap + a tiny raw-mode line editor (_pyshline),
 * needed because the static binary cannot load readline.so / termios.so.
 * Everything else lives in pysh.py (embedded via pysh_py.h). */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>
#include <locale.h>
#include <poll.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>
#include <sys/ioctl.h>

#include "pysh_py.h"

#ifndef PYSH_HOME
#define PYSH_HOME "/usr"
#endif

/* ---------------------------------------------------------------- editor */

#define MAXLINE 8192

static int key(int timeout_ms) {
    unsigned char c;
    if (timeout_ms >= 0) {
        struct pollfd p = {0, POLLIN, 0};
        if (poll(&p, 1, timeout_ms) <= 0) return -1;
    }
    for (;;) {
        ssize_t n = read(0, &c, 1);
        if (n == 1) return c;
        if (n == 0) return -2;
        if (errno != EINTR) return -2;
    }
}

static int is_cont(unsigned char c) { return (c & 0xC0) == 0x80; }
static int prev_ch(const char *b, int p) { do p--; while (p > 0 && is_cont(b[p])); return p; }
static int next_ch(const char *b, int len, int p) { do p++; while (p < len && is_cont(b[p])); return p; }

static void out(const char *s, size_t n) {
    while (n) { ssize_t w = write(1, s, n); if (w <= 0) { if (errno == EINTR) continue; return; } s += w; n -= w; }
}
static void outs(const char *s) { out(s, strlen(s)); }

static PyObject *g_suggest, *g_highlight;
static int m_active, m_search;
static PyObject *g_search;
static char g_sug[MAXLINE];   /* current suggestion tail (only when cursor at end) */

static void redraw(const char *prompt, const char *buf, int len, int pos) {
    int back = 0;
    for (int i = pos; i < len; i = next_ch(buf, len, i)) back++;
    g_sug[0] = 0;
    if (pos == len && len > 0 && g_suggest != Py_None && !m_active) {
        PyObject *r = PyObject_CallFunction(g_suggest, "s#", buf, (Py_ssize_t)len);
        if (r) {
            const char *t = PyUnicode_Check(r) ? PyUnicode_AsUTF8(r) : NULL;
            if (t && strlen(t) < MAXLINE) strcpy(g_sug, t);
            Py_DECREF(r);
        } else PyErr_Clear();
    }
    outs("\r"); outs(prompt);
    PyObject *h = g_highlight != Py_None ? PyObject_CallFunction(g_highlight, "s#", buf, (Py_ssize_t)len) : NULL;
    const char *hs = h && PyUnicode_Check(h) ? PyUnicode_AsUTF8(h) : NULL;
    if (hs) outs(hs); else { PyErr_Clear(); out(buf, len); }
    Py_XDECREF(h);
    if (g_sug[0]) {
        outs("\x1b[90m"); outs(g_sug); outs("\x1b[0m");
        for (const char *p = g_sug; *p; p++) if (!is_cont((unsigned char)*p)) back++;
    }
    outs("\x1b[K");
    if (back) { char t[32]; snprintf(t, sizeof t, "\x1b[%dD", back); outs(t); }
}

static void ins(char *buf, int *len, int *pos, const char *s, int n) {
    if (*len + n >= MAXLINE) return;
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

/* ------------------------------------------------------ completion menu */
static PyObject *m_items;          /* list of (full_before_cursor, display, desc) */
static Py_ssize_t m_n;
static int m_sel, m_rows;
static char m_tail[MAXLINE]; static int m_taillen;
static char m_orig[MAXLINE]; static int m_origlen, m_origpos;

static int cps(const char *s) { int n = 0; for (; *s; s++) if (!is_cont((unsigned char)*s)) n++; return n; }
static void term_size(int *w, int *h) {
    struct winsize ws;
    *w = 80; *h = 24;
    if (ioctl(1, TIOCGWINSZ, &ws) == 0) { if (ws.ws_col) *w = ws.ws_col; if (ws.ws_row) *h = ws.ws_row; }
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

/* print up to `room` code points of s */
static void out_cp(const char *s, int room) {
    const char *p = s; int k = 0;
    while (*p) {
        const char *q = p + 1; while (is_cont((unsigned char)*q)) q++;
        if (++k > room) break;
        p = q;
    }
    out(s, p - s);
}

/* draw the menu under the current line, then move the cursor back up */
static void menu_draw(void) {
    int tw, th, colw = 0;
    term_size(&tw, &th);
    for (Py_ssize_t i = 0; i < m_n; i++) {
        const char *f, *d, *e; item(i, &f, &d, &e);
        int w = cps(d) + (*e ? 2 + cps(e) : 0);
        if (w > colw) colw = w;
    }
    if (colw > tw - 3) colw = tw - 3;
    int cols = tw / (colw + 2); if (cols < 1 || m_search) cols = 1;
    int rows = (int)((m_n + cols - 1) / cols);
    int maxrows = th - 3; if (maxrows < 3) maxrows = 3;
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
            int dl = cps(d), used;
            if (dl > colw) dl = colw;
            if (i == m_sel) outs("\x1b[7m");
            out_cp(d, colw);
            used = dl;
            if (*e && colw - dl > 3) {
                outs(i == m_sel ? "  " : "  \x1b[90m");
                int room = colw - dl - 2, el = cps(e); if (el > room) el = room;
                out_cp(e, room); used += 2 + el;
            }
            outs("\x1b[0m");
            pad(colw - used + 2);
        }
    }
    char t[32]; snprintf(t, sizeof t, "\x1b[%dA", shown); outs(t);
    m_rows = shown;
}
static void menu_clear(void) {
    if (m_rows) outs("\r\n\x1b[J\x1b[A");
    m_rows = 0;
}
static void menu_close(void) {
    menu_clear();
    m_active = 0; m_search = 0;
    Py_CLEAR(m_items);
}

/* Ctrl-R: re-run the history query for what's in the buffer and redraw the menu */
static void search_refresh(const char *prompt, const char *buf, int len, int pos) {
    menu_clear();
    Py_CLEAR(m_items);
    PyObject *r = PyObject_CallFunction(g_search, "s#", buf, (Py_ssize_t)len);
    if (!r || !PyList_Check(r)) { PyErr_Clear(); Py_XDECREF(r); r = PyList_New(0); }
    m_items = r; m_n = PyList_Size(r);
    m_sel = m_n ? 0 : -1;
    menu_draw();
    redraw(prompt, buf, len, pos);
}

/* readline(prompt, history_list, completer) -> str
 * completer(text_before_cursor) -> (replacement_for_that_text, list_or_None) */
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

    static char buf[MAXLINE], saved[MAXLINE];
    int len = 0, pos = 0;
    Py_ssize_t nh = PyList_Size(hist), hi = nh;
    PyObject *result = NULL;
    int saved_len = 0;
    buf[0] = 0;
    outs("\r"); outs(prompt);

    for (;;) {
        int c = key(-1);
        if (c == -2) { PyErr_SetString(PyExc_EOFError, "eof"); break; }
        int changed = 1;
        if (m_active && m_search && (c == '\r' || c == '\n')) {      /* take the chosen history entry */
            if (m_sel >= 0) {
                const char *f, *d, *e; item(m_sel, &f, &d, &e);
                int n = (int)strnlen(f, MAXLINE - 1); memcpy(buf, f, n); len = pos = n;
            }
            menu_close(); redraw(prompt, buf, len, pos); continue;
        }
        if (m_active && m_search && c == 3) { menu_close(); redraw(prompt, buf, len, pos); }
        if (m_active && !m_search && c != 9 && c != 27 && !((c == '\r' || c == '\n') && m_sel >= 0)) {
            menu_close(); redraw(prompt, buf, len, pos);
        }
        if (m_active && !m_search && (c == '\r' || c == '\n')) {          /* accept selection, don't run */
            menu_close(); redraw(prompt, buf, len, pos); continue;
        }
        switch (c) {
        case '\r': case '\n':
            { PyObject *sv = g_suggest; g_suggest = Py_None; redraw(prompt, buf, len, len); g_suggest = sv; }
            outs("\r\n");
            result = PyUnicode_DecodeUTF8(buf, len, "replace");
            goto done;
        case 3: /* ^C */
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
        case 12: outs("\x1b[H\x1b[2J"); break;           /* ^L */
        case 127: case 8:
            if (pos > 0) del(buf, &len, prev_ch(buf, pos), pos, &pos);
            break;
        case 20: /* ^T transpose */
            changed = 0; break;
        case 16: case 14: { /* ^P ^N */
            goto hist_nav_dispatch;
        }
        case 18: /* ^R: history search menu */
            if (m_active && m_search) { m_sel = m_n ? (int)((m_sel + 1) % m_n) : -1; goto menu_select; }
            if (g_search == Py_None) { changed = 0; break; }
            memcpy(m_orig, buf, len); m_origlen = len; m_origpos = pos; m_taillen = 0;
            m_active = 1; m_search = 1; m_n = 0;
            search_refresh(prompt, buf, len, pos);
            changed = 0; break;
        case 9: { /* TAB: complete / cycle the menu */
            if (m_active) { m_sel = (int)((m_sel + 1) % m_n); goto menu_select; }
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
                if (m_active) { menu_draw(); redraw(prompt, buf, len, pos); changed = 0; }
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
                int w, h; term_size(&w, &h);
                if (m_sel < 0) m_sel = (c2 == 'D' || c2 == 'A' || c2 == 'Z') ? (int)m_n - 1 : 0;
                else if (c2 == 'C' || c2 == 'B') m_sel = (int)((m_sel + 1) % m_n);
                else m_sel = (int)((m_sel + m_n - 1) % m_n);
                goto menu_select;
            }
            if (m_active && !m_search) { menu_close(); redraw(prompt, buf, len, pos); }
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
                char ch[4]; int n = 1; ch[0] = (char)c;
                if (c >= 0xC0) { int more = c >= 0xF0 ? 3 : c >= 0xE0 ? 2 : 1; for (int i = 0; i < more; i++) { int x = key(-1); if (x < 0) break; ch[n++] = (char)x; } }
                ins(buf, &len, &pos, ch, n);
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
        {
            const char *f, *d, *e; item(m_sel, &f, &d, &e);
            int n = strlen(f);
            if (!m_search && n + m_taillen < MAXLINE) { memcpy(buf, f, n); memcpy(buf + n, m_tail, m_taillen); len = n + m_taillen; pos = n; }
            menu_clear();
            menu_draw(); redraw(prompt, buf, len, pos);
            changed = 0;
        }
        goto skip_redraw;
    redraw_now:
        if (changed) redraw(prompt, buf, len, pos);
        if (m_active && m_search) search_refresh(prompt, buf, len, pos);
    skip_redraw: ;
    }
done:
    if (m_active) menu_close();
    tcsetattr(0, TCSADRAIN, &old);
    return result;
}

static PyMethodDef le_methods[] = {
    {"readline", le_readline, METH_VARARGS, "readline(prompt, history, completer)"},
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

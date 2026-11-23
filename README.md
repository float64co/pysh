# pysh

A shell and a Python REPL in one executable. Every line is Python if it parses
as Python, and a shell command otherwise. It embeds the CPython that is installed
on your system, so `import` finds the same modules your `python` does.

![pysh screenshot](screenshot.png)

```
~/s/c/pysh(main)> x = 6 * 7
~/s/c/pysh(main)> x
42
~/s/c/pysh(main)> ls -la | wc -l
12
~/s/c/pysh(main)> files = $(ls).split()
```

## Build

```sh
make            # pysh         - dynamic glibc; loads any C extension your python can
make static     # pysh-static  - fully static; no C-extension modules (ssl, sqlite3, lxml...)
make install    # PREFIX=/usr/local by default; also installs the man page
```

Needs `gcc`, `xxd`, and the development files for the Python you want to embed
(`libpython3.x.a`, headers), plus `libexpat`, `zlib` and OpenSSL headers.
`make PYTHON=python3.11` builds against another interpreter. The Python home is baked in
at build time and can be overridden with `PYSH_HOME`.

## Usage

```
pysh                   interactive
pysh -c 'cmd'          run a command string and exit
pysh script [args]     run a file of mixed shell/Python lines (or make it executable, see Scripts)
pysh -e ...            stop at the first failing command (scripts and -c)
pysh --help | --version
```

`help` inside the shell prints the same help; `man pysh` has the full details.

## Python and shell together

| Syntax      | Meaning                                                     |
|-------------|-------------------------------------------------------------|
| `$(cmd)`    | stdout of `cmd` as a `str`                                  |
| `!(cmd)`    | a `Result`: `.returncode .out .err .duration`               |
| `@(expr)`   | splice a Python value into a command (lists become words)   |
| `$VAR`      | environment variable; `$VAR = "x"` sets it                  |
| `![cmd]`    | a `Process` you read while it runs: `for line in ![tail -f log]:` |
| `$[cmd]`    | run attached to the terminal (live; interactive programs work), returns a `Result` |
| `% cmd`     | a leading `%` forces shell mode: `% X=1; make`                      |
| `last`      | the `Result` of the previous command                        |
| `aliases`   | dict; `aliases["ll"] = "ls -l"` or a function(args)         |

Inside functions, `@(expr)` sees the function's local variables. `$name` in a shell line is an
*environment* variable; use `@(name)` for a Python variable.

A line is treated as a command when it doesn't parse as Python, or when it starts with
a name that isn't bound in Python but is a program, builtin or alias (`ls -la`, `id -u`).

The shell side has pipes (`|`, and `|&` to include stderr), `&& || ; &`, redirects
(`> >> < 2> 2>&1 >&2 &>`), quotes, globs, `~`, `$VAR`, `VAR=x cmd`, brace expansion
(`{a,b}`, `{1..10..2}`, `{01..12}`), arithmetic (`$((1 + 2 * $X))`), here-documents
(`<<EOF`, `<<-EOF`, `<<'EOF'`) and here-strings (`<<< text`). There are no shell loops or
functions; use Python.
Builtins: `cd pwd exit export unset alias unalias jobs fg bg wait kill source history type help`.

### Forcing shell mode with `%`

pysh guesses whether a line is Python or shell. A leading `%` removes the guess: the line is a shell
command, whatever it looks like.

```
% export X=5; sh -c 'echo $X'      # one-line assignment + command (this parses as Python otherwise)
% echo 1 + 2                       # prints "1 + 2" instead of evaluating it
```

`%` is only special at the start of a line, so `x % 4` is still modulo. Highlighting and Tab
completion understand it.

### Streaming output

`$(cmd)` and `!(cmd)` wait for the command to finish. To read output while it is still running, use
`![cmd]`, which returns a `Process` immediately:

```python
for line in ![tail -f /var/log/syslog]:          # lines arrive as they are printed
    if "error" in line: print(line)

with ![make -j8] as p:                           # the command is ended if you leave the block early
    for line in p:
        print(line)
print(p.returncode)
```

| `Process` member | |
|---|---|
| iteration, `readline()`, `read()` | lines without the newline / one line / everything left |
| `wait(timeout=None)` | wait for it to exit, return the exit status |
| `poll()`, `.returncode` | status if it has exited, else `None` |
| `terminate()`, `kill()`, `send_signal(sig)` | signal the whole pipeline |
| `.result` | a `Result` (after it has exited) |
| `.pid`, `.timed_out` | |

Standard error stays on the terminal (`sh(cmd, stream=True, merge=True)` folds it into the stream).
`![...]` streams one pipeline; for `;`, `&&` or `&` lists use a normal command line.

`$[cmd]` runs a command attached to the terminal: output is live, interactive programs work
(`$[vim notes.txt]`, `Ctrl-Z` stops them) and you get a `Result` back.

`sh()` is the same machinery with options, for use from Python code:

```python
sh("make", tee=True)                   # show output live and capture it in the Result
sh("deploy.sh", check=True)            # raise ShellError (with .result) on a non-zero exit
sh("curl -s $URL", timeout=10)         # SIGTERM after 10 s, SIGKILL 2 s later; raises subprocess.TimeoutExpired
sh("make", capture=False)              # same as $[make]
sh("tail -f log", stream=True)         # same as ![tail -f log]
```

`timeout` ends the whole process group, so a command that spawns children can't hold the call open.

### Working directory

`$PWD` always matches the real working directory: if the parent process left an unset, relative or
stale `PWD` in the environment (for example a Windows-side path when launched through `wsl.exe`),
pysh corrects it at startup. `cd` keeps symlinks logical like bash (`cd link; cd ..` goes to the
link's parent; `cd -P` resolves them), and `pwd` is a builtin (`pwd -P` for the physical path).

**Job control**: `Ctrl-Z` stops the foreground job, `jobs`/`fg %2`/`bg`/`kill %1`/`wait`
manage it, `cmd &` runs in the background, finished jobs are reported before the next prompt,
and `Ctrl-C` only interrupts the foreground job.

## Scripts

Start a file with a shebang and `chmod +x` it:

```python
#!/usr/bin/env pysh
import sys
for host in sys.argv[1:]:
    r = !(ping -c1 -W1 @(host))
    print(host, "up" if r else "down")
```

`sys.argv`, `__file__` and `__name__ == "__main__"` behave as in Python. An uncaught exception
prints a traceback with the real file and line and exits 1; a failing command doesn't stop the
script unless you use `#!/usr/bin/pysh -e` (or `pysh -e script`). A Python block ends at the first
unindented line, so shell lines can follow it directly.

## Interactive features

- **Autosuggestions**: grey text from history. `→`/`End`/`Ctrl-F` accept, `Alt-F` takes a word.
- **Highlighting**: valid commands blue, unknown red, strings yellow, paths underlined.
- **Tab completion menu** with descriptions: commands from man pages, files by type/size,
  Python attributes by type, flags parsed from `cmd --help`. Tab cycles, Enter runs the selection, Esc cancels.
- **`Ctrl-R`** history search menu; `↑`/`↓` search history by prefix.
- **Colored `ls`**: interactive shells alias `ls` to `ls --color=auto` when your `ls` supports it (override in `~/.pyshrc.py`).
- **Prompt**: `user@host ~/s/c/pysh(branch)>`, with the git branch in white.

## Tests

```sh
make test                      # unittest suite in tests/ (shell, parser, line editor, job control)
PYSH=./pysh-static python3 -m unittest discover -s tests -k Parser
```

The line-editor and job-control tests run pysh on a pty and render its output with a small
VT100 emulator (`tests/term.py`), so they check what a terminal would show, including wrapping.

## Configuration

`~/.pyshrc.py` runs at startup in the shell's namespace:

```python
aliases["ll"] = "ls -l"
complete_ignore = (".dll", ".exe")      # suffixes hidden from command completion
complete_ignore_dirs = ("/mnt/c",)      # PATH directories hidden from completion
def prompt(): return f"{os.getcwd()} $ "
```

Environment variables: `PYSH_HOME` (Python home, defaults to the build-time prefix),
`PYSH_HISTFILE` (history file, default `~/.pysh_history`) and `PYSH_TTYLOG=file` (copy everything the
line editor writes to the terminal into `file`, for bug reports about redraw problems).

When a command's output doesn't end in a newline, the next prompt starts on a fresh row and a
reverse-video `%` marks where the output stopped (as in zsh).

## Limitations

- Job control needs Python 3.11+ (`process_group`) and a controlling terminal.
- `make static` cannot import C-extension modules.
- The line editor assumes the terminal doesn't resize while you're typing a line.

## Examples

These mix shell output with Python libraries. Each was run in pysh.

### numpy: file-size percentiles from `stat`

```python
import numpy as np
sizes = np.array($(stat -c %s /usr/bin/*).split(), dtype=int)
np.percentile(sizes, [50, 90, 99]).astype(int)
# array([  14640,  146000, 8635872])
```

### pandas: top memory users from `ps`

```python
import io, pandas as pd
df = pd.read_csv(io.StringIO($(ps -eo comm,rss)), sep=r"\s+")
df.groupby("COMMAND").RSS.sum().nlargest(3)
# COMMAND
# claude        427932
# pysh          168740
# harvest_go    104936
```

### requests: query an API, then hand the result to a shell tool

```python
import requests
r = requests.get("https://api.github.com/repos/python/cpython")
r.json()["stargazers_count"]

echo @(r.json()["full_name"]) | tr a-z A-Z
```

Or the other way round: pipe `curl` into Python.

```
curl -s https://example.com/data.json | python3 -c "import sys,json; print(json.load(sys.stdin)['stars'])"
```

### pwntools: cyclic patterns, assembly and a live process

```python
from pwn import *
context.log_level = "error"

cyclic(24)                                  # b'aaaabaaacaaadaaaeaaafaaa'
cyclic_find(b"faaa")                        # 20

echo @(cyclic(16).decode()) | xxd | head -1 # splice a Python value into a pipeline
# 00000000: 6161 6161 6261 6161 6361 6161 6461 6161  aaaabaaacaaadaaa

disasm(asm("xor eax, eax; ret", arch="amd64"), arch="amd64")
# '   0:   31 c0                   xor    eax, eax\n   2:   c3                      ret'

p = process("/bin/cat")
p.sendline(b"hello"); p.recvline()          # b'hello\n'
p.close()

elf = ELF("/bin/ls", checksec=False)
elf.arch, elf.pie, hex(elf.entry)           # ('amd64', True, '0x6d30')
```

## License

MIT; see [LICENSE](LICENSE).

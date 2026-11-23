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
pysh script            run a file of mixed shell/Python lines
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
| `last`      | the `Result` of the previous command                        |
| `aliases`   | dict; `aliases["ll"] = "ls -l"` or a function(args)         |

A line is treated as a command when it doesn't parse as Python, or when it starts with
a name that isn't bound in Python but is a program, builtin or alias (`ls -la`, `id -u`).

The shell side has pipes (`|`, and `|&` to include stderr), `&& || ; &`, redirects
(`> >> < 2> 2>&1 >&2 &>`), quotes, globs, `~`, `$VAR`, `VAR=x cmd`, brace expansion
(`{a,b}`, `{1..10..2}`, `{01..12}`), arithmetic (`$((1 + 2 * $X))`), here-documents
(`<<EOF`, `<<-EOF`, `<<'EOF'`) and here-strings (`<<< text`). There are no shell loops or
functions; use Python.
Builtins: `cd exit export unset alias unalias jobs fg bg wait kill source history type help`.

**Job control**: `Ctrl-Z` stops the foreground job, `jobs`/`fg %2`/`bg`/`kill %1`/`wait`
manage it, `cmd &` runs in the background, finished jobs are reported before the next prompt,
and `Ctrl-C` only interrupts the foreground job.

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

History lives in `~/.pysh_history` (`PYSH_HISTFILE` to change).

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

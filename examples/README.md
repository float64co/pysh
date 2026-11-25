# pysh examples

Eighteen working scripts that each mix shell commands and Python in one file. Every one starts with
`#!/usr/local/bin/pysh`, takes `--help`, and runs with no arguments (or fails with a clear usage
message, where it needs a target). They use only libraries that are already installed with the
system Python: pandas, numpy, scipy, rich, psutil, networkx, cryptography, lxml, jinja2, pwntools,
cupy and ollama, plus tools such as `git`, `du`, `ss`, `nmap` and `file`.

Run one directly (`./examples/diskhog.sh`) or through the interpreter (`pysh examples/diskhog.sh`).
If `pysh` isn't installed at `/usr/local/bin/pysh`, use the second form or edit the first line.

| script | what it does | pysh features it shows |
|---|---|---|
| `diskhog.sh` | biggest directories under a path, and how they grew since last run | `!(du …)` into pandas, sqlite history |
| `procwatch.sh` | top processes, per-user totals, runaway-process exit status | `$(hostname)` mixed with psutil |
| `listeners.sh` | listening sockets and how exposed each one is | `!(ss …)` parsed into a DataFrame |
| `certcheck.sh` | days until TLS certificates expire; `--demo` works offline | threads, `ssl`, `cryptography` |
| `gitstats.sh` | authors, activity by weekday/hour, churn, biggest commits | one `git log --numstat` into pandas |
| `csvprofile.sh` | types, missing values, distributions, outliers, correlations | pandas + scipy, text histograms |
| `abtest.sh` | picks and runs the right significance test, with a bootstrap CI | scipy, numpy |
| `flaky.sh` | runs a command N times in parallel and measures how flaky it is | `sh(cmd, timeout=…)` in a thread pool |
| `hashsweep.sh` | hashes, duplicates, IOC matches, high-entropy files | `!(file -b …)` for the suspicious ones |
| `authlog.sh` | failed SSH logins by source, bursts, suggested (never run) blocks | log parsing, `journalctl` fallback |
| `depgraph.sh` | import graph, cycles, most depended-on modules, DOT output | `ast` + networkx |
| `llm-commit.sh` | drafts a commit message from `git diff --staged` with a local Ollama model | `$(git …)`, `$[git commit -e …]` |
| `portscan.sh` | connect scan, banners, nmap service detection, Markdown report | `!(nmap …)` + lxml + jinja2 |
| `ropnotes.sh` | pwntools tour of an ELF: checksec, imports, cyclic patterns, asm/disasm | `$(file -b …)` + pwntools |
| `gpubench.sh` | NumPy on the CPU vs CuPy on the GPU: matmul, FFT, sort, reduction, fused kernel; `--mandelbrot` draws a fractal | `!(nvidia-smi … \| @(csv.reader)).value`, cupy, same-answer checks |
| `logwatch.sh` | follow a log, filter it, count by level | a pipeline made only of Python: `@source \| @matching \| @report` |
| `apipeek.sh` | fetch JSON and describe its shape | `curl … \| raw=@ \| data=@as_json`, nothing assigned if the fetch fails |
| `inventory.sh` | memory by user, biggest processes, full disks | `ps … \| procs=@parse_ps`, `@(rows) \| sort \| head`, `$[…]` inside a block |

## Things to know

- **Safe by default.** `portscan.sh` only scans loopback and private addresses unless you pass
  `--i-am-authorized`. `authlog.sh` prints firewall commands but never runs them. `ropnotes.sh` and
  `hashsweep.sh` only read files. `gpubench.sh` needs a CUDA GPU and says so (exit 1) if there isn't one. `llm-commit.sh` talks only to your local Ollama server and only
  commits if you pass `--commit` (and then opens your editor first).
- **Untrusted text is neutralised.** File names, log lines, banners, CSV headers and model output can
  be written by someone else, so every script strips control characters and escapes rich markup
  before showing them.
- **Exit status means something.** For example `certcheck.sh` exits 1 when a certificate is close to
  expiry, `listeners.sh --fail-exposed` exits 1 when something listens beyond loopback, `flaky.sh`
  exits 1 if any run failed. That makes them usable in cron and CI.
- **Tests.** `tests/test_examples.py` runs each script (`make test`).

## A taste of the style

```python
def scan(path, depth):
    r = !(du -x -k -d @(str(depth)) @(path))      # run a command, get a Result
    for line in r.out.splitlines():               # …and use it as ordinary Python
        size, _, name = line.partition("\t")
```

`!(cmd)` captures output, `$(cmd)` returns it as a string, `$[cmd]` runs attached to the terminal,
and `@(expr)` splices a Python value into a command safely, without word-splitting or quoting bugs.

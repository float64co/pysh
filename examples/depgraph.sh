#!/usr/local/bin/pysh
"""depgraph - the import graph of a Python source tree.

Parses every .py file under a directory (it never imports or runs them), builds a directed graph of
which module imports which, and reports import cycles, the most depended-on modules, the modules
with the most dependencies, and the third-party packages used. Optionally writes Graphviz DOT.
"""
import argparse, ast, collections, os, re, sys
import networkx as nx
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", "build", "dist", ".mypy_cache", ".eggs"}

def module_name(root, path):
    """dotted name of a file; if ROOT is itself a package (has __init__.py) names start with its directory name"""
    base = os.path.dirname(os.path.abspath(root)) if os.path.exists(os.path.join(root, "__init__.py")) else root
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))[:-3].split(os.sep)
    if rel[-1] == "__init__": rel = rel[:-1]
    return ".".join(rel)

def import_time_nodes(tree):
    """import statements that run when the module is imported: not inside a function body, not under `if TYPE_CHECKING`"""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): continue
            if isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
                stack.extend(child.orelse); continue
            if isinstance(child, (ast.Import, ast.ImportFrom)): yield child
            else: stack.append(child)

def imports_of(path, mod, is_pkg, lazy):
    """names a file imports, as absolute dotted names (relative imports resolved against `mod`)"""
    try:
        with open(path, "rb") as f: tree = ast.parse(f.read(), path)
    except (SyntaxError, ValueError, OSError):
        return None
    found = []
    pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
    for node in (ast.walk(tree) if lazy else import_time_nodes(tree)):
        if isinstance(node, ast.Import):
            found += [(a.name, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[:len(pkg) - (node.level - 1)] if node.level - 1 <= len(pkg) else []
                name = ".".join(base + ([node.module] if node.module else []))
            else:
                name = node.module or ""
            if name: found.append((name, [a.name for a in node.names]))
    return found

def build(root, lazy):
    files = {}
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP and not x.startswith("."))
        for n in sorted(names):
            if n.endswith(".py"):
                p = os.path.join(d, n); files[module_name(root, p)] = p
    g = nx.DiGraph(); g.add_nodes_from(files)
    by_path = {os.path.abspath(p): m for m, p in files.items()}
    external, unparsable = collections.Counter(), []
    for mod, path in files.items():
        found = imports_of(path, mod, os.path.basename(path) == "__init__.py", lazy)
        if found is None: unparsable.append(path); continue
        for name, members in found:
            targets = [name] + [f"{name}.{m}" for m in members]           # `from pkg import sub` may import a module
            hits = {t for t in targets if t in files}
            if not hits:                                                   # the longest known prefix, e.g. pkg.mod.attr -> pkg.mod
                parts = name.split(".")
                hits = {".".join(parts[:i]) for i in range(len(parts), 0, -1) if ".".join(parts[:i]) in files}
                hits = {max(hits, key=len)} if hits else set()
            if not hits and name:                                          # `import helper` run from the same directory
                sib = by_path.get(os.path.abspath(os.path.join(os.path.dirname(path), name.split(".")[0] + ".py")))
                if sib: hits = {sib}
            for h in hits:
                if h != mod: g.add_edge(mod, h)
            if not hits and name and not name.startswith("."): external[name.split(".")[0]] += 1
    return g, external, unparsable

def main(argv=None):
    ap = argparse.ArgumentParser(prog="depgraph", description="Analyse the import graph of a Python source tree.",
                                 epilog="examples:  depgraph   |   depgraph src/ --dot graph.dot && dot -Tsvg graph.dot -o graph.svg   |   depgraph --fail-on-cycles")
    ap.add_argument("root", nargs="?", default=".", help="source directory (default: current directory)")
    ap.add_argument("-n", "--top", type=int, default=8, help="rows per table (default 8)")
    ap.add_argument("--dot", metavar="FILE", help="also write the graph in Graphviz DOT format")
    ap.add_argument("--include-lazy", action="store_true", help="also count imports inside functions and `if TYPE_CHECKING` blocks (default: import-time only)")
    ap.add_argument("--fail-on-cycles", action="store_true", help="exit 1 if there are import cycles (for CI)")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.root): sys.exit(f"depgraph: {a.root}: not a directory")
    g, external, bad = build(a.root, a.include_lazy)
    console = Console()
    if g.number_of_nodes() == 0:
        console.print(f"no Python files under {safe(a.root)}"); return 0
    console.print(f"[bold]{g.number_of_nodes()} modules[/bold], {g.number_of_edges()} internal imports"
                  + (f"  [dim]({len(bad)} files could not be parsed)[/dim]" if bad else ""))

    cycles = [sorted(c) for c in nx.strongly_connected_components(g) if len(c) > 1]
    if cycles:
        console.print(f"[bold red]{len(cycles)} import cycle(s)[/bold red]")
        for c in sorted(cycles, key=len, reverse=True)[:a.top]:
            sub = g.subgraph(c); loop = nx.find_cycle(sub)
            console.print("  " + " → ".join([safe(e[0]) for e in loop] + [safe(loop[0][0])]) + (f"   [dim](+{len(c) - len(loop)} more modules tangled in)[/dim]" if len(c) > len(loop) else ""))
    else:
        console.print("[green]no import cycles[/green]")

    for title, key in (("most depended on", g.in_degree), ("imports the most", g.out_degree)):
        t = Table(title=title, title_justify="left")
        t.add_column("module", overflow="fold"); t.add_column("imported by" if key is g.in_degree else "imports", justify="right")
        for name, n in sorted(key, key=lambda kv: (-kv[1], kv[0]))[:a.top]:
            if n: t.add_row(safe(name), str(n))
        if t.row_count: console.print(t)

    if external:
        std = sys.stdlib_module_names
        third = [(k, v) for k, v in external.most_common() if k not in std]
        lib = [(k, v) for k, v in external.most_common() if k in std]
        if third: console.print("[bold]third-party:[/bold] " + ", ".join(f"{safe(k)} ({v})" for k, v in third[:a.top * 2]))
        if lib: console.print("[bold]standard library:[/bold] " + ", ".join(f"{safe(k)} ({v})" for k, v in lib[:a.top * 2]))

    if a.dot:
        try:
            with open(a.dot, "w") as f:
                f.write("digraph imports {\n  rankdir=LR; node [shape=box, fontname=monospace];\n")
                for n in g.nodes: f.write(f'  "{n}";\n')
                for u, v in g.edges: f.write(f'  "{u}" -> "{v}";\n')
                f.write("}\n")
        except OSError as e: sys.exit(f"depgraph: {a.dot}: {e.strerror}")
        console.print(f"[dim]wrote {safe(a.dot)}[/dim]")
    return 1 if (cycles and a.fail_on_cycles) else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

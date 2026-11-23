#!/usr/local/bin/pysh
"""csvprofile - a first look at a CSV file before you open a notebook.

Column types, missing values, distributions (with tiny text histograms), outliers, strongly
correlated pairs, and the commonest values of text columns. With no file it profiles a small
synthetic dataset so you can see what the output looks like.
"""
import argparse, re, sys
import numpy as np
import pandas as pd
from scipy import stats
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table


def safe(s):
    """text from outside (file names, log lines, banners...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

BARS = "▁▂▃▄▅▆▇█"

def hist(values, bins=10):
    counts, _ = np.histogram(values, bins=bins)
    top = counts.max() or 1
    return "".join(BARS[min(7, int(c / top * 7.999))] if c else " " for c in counts)

def demo():
    rng = np.random.default_rng(7)
    n = 400
    age = rng.normal(40, 12, n).clip(18, 90).round()
    income = (age * 900 + rng.lognormal(10, 0.6, n)).round(2)
    df = pd.DataFrame({"age": age, "income": income, "visits": rng.poisson(3, n),
                       "plan": rng.choice(["free", "pro", "team"], n, p=[0.6, 0.3, 0.1]),
                       "country": rng.choice(["US", "DE", "JP", "BR", "IN"], n),
                       "score": rng.uniform(0, 1, n)})
    df["spend"] = (df.income * 0.02 + df.visits * 5 + rng.normal(0, 20, n)).round(2)
    df.loc[rng.choice(n, 25, replace=False), "income"] = np.nan
    df.loc[rng.choice(n, 4, replace=False), "spend"] *= 15                 # a few outliers
    return df

def fmt(x):
    """compact number: 0.0891, 38.9, 59.4k, 1.2M"""
    if pd.isna(x): return "-"
    for limit, div, suffix in ((1e6, 1e6, "M"), (1e3, 1e3, "k")):
        if abs(x) >= limit: return f"{x / div:.3g}{suffix}"
    return f"{x:.2g}" if abs(x) < 10 else f"{x:.3g}"

def main(argv=None):
    ap = argparse.ArgumentParser(prog="csvprofile", description="Profile the columns of a CSV file.",
                                 epilog="examples:  csvprofile data.csv   |   csvprofile data.csv --sep ';' --corr 0.5   |   cat data.csv | csvprofile -")
    ap.add_argument("file", nargs="?", help="CSV file, or - for standard input (default: a built-in demo dataset)")
    ap.add_argument("--sep", default=",", help="field separator (default ',')")
    ap.add_argument("--corr", type=float, default=0.7, help="report numeric pairs with |r| at least this (default 0.7)")
    ap.add_argument("--top", type=int, default=5, help="values to list for text columns (default 5)")
    a = ap.parse_args(argv)

    console = Console()
    if a.file is None:
        df = demo(); console.print("[dim]no file given: profiling a synthetic demo dataset[/dim]")
    else:
        try: df = pd.read_csv(sys.stdin if a.file == "-" else a.file, sep=a.sep)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
            sys.exit(f"csvprofile: {a.file}: {e}")
    if df.empty and not len(df.columns): sys.exit("csvprofile: no data")

    mem = df.memory_usage(deep=True).sum() / 1e6
    console.print(f"[bold]{len(df):,} rows × {len(df.columns)} columns[/bold]  ({mem:.1f} MB)  "
                  f"{int(df.duplicated().sum())} duplicate rows")

    t = Table(title="columns", title_justify="left")
    for c, j in (("column", "left"), ("type", "left"), ("missing", "right"), ("unique", "right")): t.add_column(c, justify=j)
    for c in df.columns:
        miss = int(df[c].isna().sum())
        t.add_row(safe(c), str(df[c].dtype), f"{miss} ({miss / len(df):.0%})" if miss else "0", f"{df[c].nunique():,}")
    console.print(t)

    num = df.select_dtypes("number")
    if not num.empty:
        t = Table(title="numeric columns", title_justify="left", box=box.SIMPLE_HEAD, pad_edge=False)
        for c, j in (("column", "left"), ("mean", "right"), ("std", "right"), ("min", "right"), ("med", "right"),
                     ("max", "right"), ("skew", "right"), ("out", "right"), ("shape", "left")):
            t.add_column(c, justify=j, no_wrap=True)
        for c in num.columns:
            x = num[c].dropna()
            if x.empty: continue
            q1, q3 = x.quantile([0.25, 0.75]); iqr = q3 - q1
            out = int(((x < q1 - 1.5 * iqr) | (x > q3 + 1.5 * iqr)).sum()) if iqr > 0 else 0
            skew = stats.skew(x) if len(x) > 2 and x.std() > 0 else float("nan")
            t.add_row(safe(c), fmt(x.mean()), fmt(x.std()), fmt(x.min()), fmt(x.median()), fmt(x.max()),
                      f"{skew:+.2f}", str(out) if out else "-", hist(x))
        console.print(t)
        console.print("[dim]out = outliers, beyond 1.5 × IQR from the quartiles[/dim]")

        if num.shape[1] > 1:
            r = num.corr()
            pairs = [(abs(r.iloc[i, j]), r.iloc[i, j], r.columns[i], r.columns[j])
                     for i in range(len(r)) for j in range(i + 1, len(r)) if abs(r.iloc[i, j]) >= a.corr]
            if pairs:
                t = Table(title=f"correlated pairs (|r| ≥ {a.corr})", title_justify="left")
                t.add_column("a"); t.add_column("b"); t.add_column("r", justify="right")
                for _, v, x, y in sorted(pairs, reverse=True): t.add_row(safe(x), safe(y), f"{v:+.2f}")
                console.print(t)
            else:
                console.print(f"[dim]no numeric pair has |r| ≥ {a.corr}[/dim]")

    txt = df.select_dtypes(exclude="number")
    for c in txt.columns:
        vc = df[c].value_counts().head(a.top)
        if vc.empty: continue
        console.print(f"[bold]{safe(c)}[/bold]  " + "   ".join(f"{safe(k)}: {v} ({v / len(df):.0%})" for k, v in vc.items()))

if __name__ == "__main__":
    main(sys.argv[1:])

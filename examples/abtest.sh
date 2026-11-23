#!/usr/local/bin/pysh
"""abtest - did variant B really differ from variant A?

Picks a sensible test for the data (Welch t-test, Mann-Whitney U, or a proportions test for 0/1
outcomes), and reports the p-value, an effect size, and a bootstrap confidence interval for the
difference. It warns when the sample is too small to trust.
"""
import argparse, os, re, sys
import numpy as np
from scipy import stats


def clean(s):
    """text from outside made safe to print: control characters (terminal escapes) become '?', newlines and tabs are kept"""
    return re.sub(r"[\x00-\x08\x0b-\x0c\x0d-\x1f\x7f-\x9f]", "?", str(s))

def load(spec, column):
    """A path to a file (one number per line, or a CSV column with --column) or a comma-separated list."""
    if os.path.exists(spec):
        if column is not None:
            import pandas as pd
            return pd.read_csv(spec)[column].dropna().to_numpy(float)
        with open(spec) as f: return np.array([float(l) for l in f.read().replace(",", "\n").split() if l.strip()])
    return np.array([float(v) for v in spec.split(",") if v.strip()])

def from_csv(path, group, value):
    import pandas as pd
    df = pd.read_csv(path)
    groups = [g for g, _ in df.groupby(group)]
    if len(groups) != 2: sys.exit(f"abtest: column {group!r} must have exactly two groups, found {len(groups)}: {groups[:6]}")
    return [df.loc[df[group] == g, value].dropna().to_numpy(float) for g in groups], [clean(g) for g in groups]

def demo():
    rng = np.random.default_rng(42)
    return [rng.normal(100, 15, 100), rng.normal(110, 15, 110)]

def bootstrap_diff(a, b, stat, n, seed):
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(a), (n, len(a))); ib = rng.integers(0, len(b), (n, len(b)))
    d = stat(b[ib], axis=1) - stat(a[ia], axis=1)
    return np.percentile(d, [2.5, 97.5])

def main(argv=None):
    ap = argparse.ArgumentParser(prog="abtest", description="Compare two samples and say whether they differ.",
                                 epilog="examples:  abtest --a 12,15,11,14 --b 18,17,20,16   |   abtest --csv results.csv --group variant --value revenue   |   abtest   (demo data)")
    ap.add_argument("--a", help="sample A: a file or a comma-separated list")
    ap.add_argument("--b", help="sample B: a file or a comma-separated list")
    ap.add_argument("--column", help="read --a/--b files as CSV and take this column")
    ap.add_argument("--csv", help="a CSV holding both samples (use with --group and --value)")
    ap.add_argument("--group", help="column naming the variant (exactly two values)")
    ap.add_argument("--value", help="column holding the measurement")
    ap.add_argument("--alpha", type=float, default=0.05, help="significance level (default 0.05)")
    ap.add_argument("--boot", type=int, default=5000, help="bootstrap resamples (default 5000)")
    ap.add_argument("--seed", type=int, default=0, help="random seed for the bootstrap (default 0)")
    a = ap.parse_args(argv)

    names = ["A", "B"]
    if a.csv:
        if not (a.group and a.value): ap.error("--csv needs --group and --value")
        (x, y), names = from_csv(a.csv, a.group, a.value)
    elif a.a and a.b:
        try: x, y = load(a.a, a.column), load(a.b, a.column)
        except (OSError, ValueError, KeyError) as e: sys.exit(f"abtest: {e}")
    elif a.a or a.b: ap.error("give both --a and --b")
    else:
        x, y = demo(); print("(no data given: using a synthetic demo, B is about 10 higher than A)\n")
    if len(x) < 2 or len(y) < 2: sys.exit("abtest: each sample needs at least two values")

    print(f"{names[0]}: n={len(x)} mean={x.mean():.4g} sd={x.std(ddof=1):.4g}")
    print(f"{names[1]}: n={len(y)} mean={y.mean():.4g} sd={y.std(ddof=1):.4g}")
    warnings = []
    if min(len(x), len(y)) < 30: warnings.append("small sample (n < 30): treat the p-value with caution")

    binary = set(np.unique(np.concatenate([x, y]))) <= {0.0, 1.0}
    if binary:
        table = [[x.sum(), len(x) - x.sum()], [y.sum(), len(y) - y.sum()]]
        expected = stats.contingency.expected_freq(table)
        if (expected < 5).any():
            test = "Fisher's exact test (small counts)"; p = stats.fisher_exact(table)[1]
        else:
            test = "chi-squared test of proportions"; p = stats.chi2_contingency(table)[1]
        pa, pb = x.mean(), y.mean()
        effect = f"difference in rate {pb - pa:+.3f}  (relative {((pb / pa) - 1):+.1%})" if pa else f"difference in rate {pb - pa:+.3f}"
        lo, hi = bootstrap_diff(x, y, np.mean, a.boot, a.seed)
        what = "difference in rate"
    else:
        normal = all(len(s) >= 8 and len(s) <= 5000 and stats.shapiro(s)[1] > 0.05 for s in (x, y))
        if normal:
            test = "Welch's t-test (both samples look normal)"; p = stats.ttest_ind(y, x, equal_var=False)[1]
            pooled = np.sqrt((x.var(ddof=1) + y.var(ddof=1)) / 2)
            effect = f"Cohen's d = {(y.mean() - x.mean()) / pooled:+.2f}" if pooled else "Cohen's d undefined (no variance)"
            lo, hi = bootstrap_diff(x, y, np.mean, a.boot, a.seed); what = "difference in means"
        else:
            test = "Mann-Whitney U test (normality not supported)"; p = stats.mannwhitneyu(y, x, alternative="two-sided")[1]
            u = stats.mannwhitneyu(y, x)[0]
            effect = f"rank-biserial r = {2 * u / (len(x) * len(y)) - 1:+.2f}"
            lo, hi = bootstrap_diff(x, y, np.median, a.boot, a.seed); what = "difference in medians"

    print(f"\ntest:    {test}")
    print(f"p-value: {p:.4g}")
    print(f"effect:  {effect}")
    print(f"95% bootstrap CI for the {what} (B - A): [{lo:+.4g}, {hi:+.4g}]")
    for w in warnings: print(f"warning: {w}")
    sig = p < a.alpha
    print(f"\nverdict: {'a difference is supported' if sig else 'no reliable difference found'} at alpha = {a.alpha}")
    if not sig and (hi - lo) > 0: print("         (absence of evidence is not evidence of absence: check the interval's width)")
    return 0 if sig else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/local/bin/pysh
"""gpubench - how much faster is your GPU than your CPU, on real array workloads?

Runs the same NumPy code on the CPU and CuPy code on the GPU (matrix multiply, FFT, sort, a
reduction, and a fused element-wise kernel), checks that both give the same answer, and prints a
table of times and speedups. `--mandelbrot` instead computes a fractal on both and draws it in
your terminal. GPU facts come from `nvidia-smi`. Exits 1 if there is no usable CUDA GPU or a result
differs.
"""
import argparse, csv, re, shutil, statistics, sys, time
import numpy as np
from rich.console import Console
from rich.markup import escape
from rich.table import Table

def safe(s):
    """text from outside (driver strings...) made safe to show: no control characters, no rich markup"""
    return escape(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", str(s)))

KERNELS = ("matmul", "fft", "sort", "sum", "fused")

def gpu_facts():
    """one line per GPU from nvidia-smi, as dicts (empty if nvidia-smi is missing)"""
    if not shutil.which("nvidia-smi"): return []
    # a Python stage in the pipeline: csv does the parsing, and `.value` hands back the rows
    r = !(nvidia-smi --query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu,driver_version --format=csv,noheader,nounits | @(lambda f: csv.reader(f, skipinitialspace=True)))
    keys = ("name", "total", "used", "temp", "util", "driver")
    return [dict(zip(keys, (x.strip() for x in row))) for row in r.value or [] if len(row) == len(keys)]

def load_cupy():
    """-> cupy module, or exit 1 with a helpful message"""
    try:
        import cupy as cp
    except ImportError:
        sys.exit("gpubench: cupy is not installed (pip install cupy-cuda12x, or the build matching your CUDA version)")
    try:
        n = cp.cuda.runtime.getDeviceCount()
    except Exception:
        n = 0
    if n < 1: sys.exit("gpubench: no CUDA GPU is visible to cupy (is the driver installed and CUDA_VISIBLE_DEVICES unset?)")
    return cp

def bench(fn, sync, repeat):
    """median seconds over `repeat` runs, after one untimed warm-up (kernel compilation, FFT plans, caches)"""
    fn(); sync()
    times = []
    for _ in range(repeat):
        sync(); t = time.perf_counter(); fn(); sync(); times.append(time.perf_counter() - t)
    return statistics.median(times)

def same(a, b, dtype):
    """results agree within what the precision allows (relative to the largest value)"""
    a, b = np.asarray(a), np.asarray(b)
    tol = 1e-3 if np.dtype(dtype) == np.float32 else 1e-9
    return a.shape == b.shape and bool(np.allclose(a, b, rtol=tol, atol=tol * max(float(np.abs(b).max()), 1.0)))

def workloads(xp, x, y, m, n, dtype):
    """the same five jobs for numpy (xp=np) or cupy (xp=cp): name -> (callable, flops or None)"""
    a, b = m
    fused = None
    if xp is not np:
        fused = xp.ElementwiseKernel("T x, T y", "T z", "z = sinf(x) * expf(-y) + x * y", "gpubench_fused") if x.dtype == np.float32 else None
    def run_fused():
        if fused is not None: return fused(x, y)
        return xp.sin(x) * xp.exp(-y) + x * y
    return {
        "matmul": (lambda: a @ b, 2.0 * n ** 3),
        "fft": (lambda: xp.fft.fft(x), None),
        "sort": (lambda: xp.sort(y), None),
        "sum": (lambda: (x.sum(), y.std()), None),
        "fused": (run_fused, None),
    }

def run_benchmarks(a, cp, console):
    dtype = np.dtype(a.dtype)
    n = a.size
    elems = n * n
    need = 14 * elems * dtype.itemsize                       # a few arrays plus FFT/sort scratch
    free, total = cp.cuda.runtime.memGetInfo()
    if need > free * 0.8:
        sys.exit(f"gpubench: --size {n} needs about {need / 2**30:.1f} GiB of GPU memory but only {free / 2**30:.1f} GiB is free; try a smaller --size")
    rng = np.random.default_rng(a.seed)
    xh = rng.random(elems, dtype=dtype); yh = rng.random(elems, dtype=dtype)
    mh = (rng.random((n, n), dtype=dtype), rng.random((n, n), dtype=dtype))
    cpu = workloads(np, xh, yh, mh, n, dtype)

    t = time.perf_counter(); xg = cp.asarray(xh); yg = cp.asarray(yh); mg = (cp.asarray(mh[0]), cp.asarray(mh[1])); cp.cuda.Device().synchronize()
    up = time.perf_counter() - t
    gb = (2 * xh.nbytes + 2 * mh[0].nbytes) / 1e9
    gpu = workloads(cp, xg, yg, mg, n, dtype)
    sync = cp.cuda.Device().synchronize

    t = Table(title=f"{n}×{n} {dtype.name} ({elems / 1e6:.1f} M elements), median of {a.repeat}", title_justify="left")
    for c, j in (("workload", "left"), ("CPU (numpy)", "right"), ("GPU (cupy)", "right"), ("speedup", "right"), ("GFLOP/s GPU", "right"), ("same answer", "left")):
        t.add_column(c, justify=j)
    bad = 0
    for name in a.kernels:
        cfn, flops = cpu[name]; gfn, _ = gpu[name]
        tc = bench(cfn, lambda: None, a.repeat); tg = bench(gfn, sync, a.repeat)
        rc, rg = cfn(), gfn()
        if isinstance(rg, tuple): rc, rg = np.array([float(v) for v in rc]), np.array([float(v.get()) for v in rg])
        else: rg = cp.asnumpy(rg)
        ok = same(rg, rc, dtype)
        bad += not ok
        t.add_row(name, f"{tc * 1e3:.1f} ms", f"{tg * 1e3:.2f} ms", f"{tc / tg:.1f}×", f"{flops / tg / 1e9:,.0f}" if flops else "-",
                  "yes" if ok else "NO", style=None if ok else "bold red")
    console.print(t)
    console.print(f"host → device copy: {gb * 1e3:.0f} MB in {up * 1e3:.0f} ms ({gb / up:.1f} GB/s). "
                  "[dim]speedups don't include that copy; data already on the GPU is the fair case[/dim]")
    if dtype == np.float64:
        console.print("[dim]float64 is slow on most consumer GPUs (often 1/32 to 1/64 of float32 speed)[/dim]")
    return bad

def mandelbrot(xp, w, h, iters):
    x = xp.linspace(-2.2, 0.8, w, dtype=xp.float32); y = xp.linspace(-1.0, 1.0, h, dtype=xp.float32)
    c = x[None, :] + 1j * y[:, None]
    z = xp.zeros_like(c); counts = xp.zeros(c.shape, dtype=xp.int32)
    for _ in range(iters):
        alive = (z.real * z.real + z.imag * z.imag) <= 4          # frozen once escaped, so nothing overflows
        z = xp.where(alive, z * z + c, z)
        counts += alive
    return counts

def draw(counts, iters, cols):
    ramp = " .,:;-=+*#%@"
    h, w = counts.shape
    sx = max(1, w // cols); sy = max(1, round(sx * 2))            # terminal cells are about twice as tall as wide
    small = counts[::sy, ::sx]
    lines = []
    for row in small:
        lines.append("".join("@" if v >= iters else ramp[int(v / iters * (len(ramp) - 2))] for v in row))
    return "\n".join(lines)

def run_mandelbrot(a, cp, console):
    w, h = a.width, a.width * 2 // 3
    t = time.perf_counter(); cpu = mandelbrot(np, w, h, a.iters); tc = time.perf_counter() - t
    mandelbrot(cp, 64, 48, 2); cp.cuda.Device().synchronize()      # warm-up
    t = time.perf_counter(); g = mandelbrot(cp, w, h, a.iters); cp.cuda.Device().synchronize(); tg = time.perf_counter() - t
    gpu = cp.asnumpy(g)
    differ = int((gpu != cpu).sum())
    cols = min(shutil.get_terminal_size((80, 24)).columns - 1, w)
    console.print(draw(gpu, a.iters, cols), highlight=False, markup=False)
    console.print(f"{w}×{h} points, {a.iters} iterations   CPU {tc:.2f} s   GPU {tg * 1e3:.0f} ms   speedup {tc / tg:.0f}×")
    frac = differ / gpu.size
    if frac > 0.001:
        console.print(f"[red]{differ} points ({frac:.2%}) differ between CPU and GPU[/red]"); return 1
    console.print(f"[dim]{differ} of {gpu.size} points differ by an iteration at the boundary (float rounding)[/dim]" if differ else "[dim]CPU and GPU agree exactly[/dim]")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpubench", description="Benchmark NumPy on the CPU against CuPy on the GPU, and check they agree.",
                                 epilog="examples:  gpubench   |   gpubench --size 4096 --kernels matmul,fft   |   gpubench --dtype float64   |   gpubench --mandelbrot --width 2400")
    ap.add_argument("-n", "--size", type=int, default=2048, help="matrix side N; the other workloads use N×N elements (default 2048)")
    ap.add_argument("-k", "--kernels", default=",".join(KERNELS), help=f"comma-separated subset of: {', '.join(KERNELS)} (default all)")
    ap.add_argument("-r", "--repeat", type=int, default=5, help="timed runs per workload; the median is reported (default 5)")
    ap.add_argument("--dtype", choices=("float32", "float64"), default="float32", help="element type (default float32)")
    ap.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    ap.add_argument("--mandelbrot", action="store_true", help="compute and draw a Mandelbrot set on both devices instead of the benchmark")
    ap.add_argument("--width", type=int, default=1200, help="--mandelbrot: grid width in points; height is two thirds of it (default 1200)")
    ap.add_argument("--iters", type=int, default=150, help="--mandelbrot: iterations per point (default 150)")
    a = ap.parse_args(argv)

    a.kernels = [k.strip() for k in a.kernels.split(",") if k.strip()]
    unknown = [k for k in a.kernels if k not in KERNELS]
    if unknown or not a.kernels: ap.error(f"unknown workload {unknown or ''}; choose from {', '.join(KERNELS)}")
    if a.size < 16 or a.repeat < 1 or a.width < 16 or a.iters < 1: ap.error("--size and --width must be at least 16, --repeat and --iters at least 1")

    console = Console()
    cp = load_cupy()
    for g in gpu_facts():
        console.print(f"[bold]{safe(g['name'])}[/bold]  {g['used']}/{g['total']} MiB in use, {g['temp']}°C, {g['util']}% busy, driver {safe(g['driver'])}")
    console.print(f"cupy {cp.__version__}, CUDA runtime {cp.cuda.runtime.runtimeGetVersion()}, numpy {np.__version__}\n")

    try:
        if a.mandelbrot: return run_mandelbrot(a, cp, console)
        bad = run_benchmarks(a, cp, console)
    except cp.cuda.memory.OutOfMemoryError:
        sys.exit("gpubench: ran out of GPU memory; try a smaller --size")
    except cp.cuda.runtime.CUDARuntimeError as e:
        sys.exit(f"gpubench: CUDA error: {e}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

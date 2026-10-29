"""
General lab-graph plotter.  CSV in, graph out.

CSV format: first row = column names, then your data.
  - By default the FIRST column is x and EVERY OTHER column is a y-series.
  - Use --x / --y to pick columns, --group to split rows into separate lines.

Examples:
  python plot.py data.csv
  python plot.py motor.csv --x rpm --y voltage --group direction --fit
  python plot.py data.csv --title "V-I characteristic" --xlabel "I (mA)" --ylabel "V (V)"
  python plot.py data.csv --xrange 0 100 --yrange 0 50
  python plot.py data.csv --save            # writes graph.png
  python plot.py data.csv --terminal        # draw in terminal (pip install plotext)
"""
import argparse
import csv
import math
import sys
from collections import defaultdict


# ----------------------------------------------------------------- loading
def load(path, xcol, ycols, group):
    with open(path, newline="") as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        # .replace: tables copied from web pages use a fancy minus sign (U+2212)
        rows = [{(k or "").strip(): (v or "").strip().replace("\u2212", "-") for k, v in r.items()}
                for r in reader]
    if not rows:
        sys.exit("The CSV has no data rows.")

    headers = list(rows[0].keys())
    xcol = xcol or headers[0]
    ycols = ycols or [h for h in headers if h not in (xcol, group)]
    for c in [xcol, *ycols] + ([group] if group else []):
        if c not in headers:
            sys.exit(f"Column '{c}' not found. Columns are: {', '.join(headers)}")

    series = defaultdict(lambda: ([], []))
    skipped = []
    for i, row in enumerate(rows, start=2):          # start=2: line 1 is the header
        for y in ycols:
            try:
                xv, yv = float(row[xcol]), float(row[y])
            except ValueError:
                skipped.append(i)
                continue
            if group:
                label = row[group] if len(ycols) == 1 else f"{y} ({row[group]})"
            else:
                label = y
            series[label][0].append(xv)
            series[label][1].append(yv)

    if skipped:
        print(f"Skipped unreadable/blank values on CSV line(s): {sorted(set(skipped))}")
    if not series:
        sys.exit("No plottable data found.")
    return xcol, ycols, {k: sorted(zip(*v)) for k, v in series.items()}


# ----------------------------------------------------------------- helpers
def nice_limits(lo, hi, target_ticks=6):
    """Round the axis range outward to neat numbers (steps of 1, 2, 5 x 10^n)."""
    span = (hi - lo) or abs(hi) or 1.0
    raw = span / target_ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def linear_fit(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    m = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    c = my - m * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (m * x + c)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return m, c, r2


def ranges(data, xrange_, yrange_):
    xs = [p[0] for pts in data.values() for p in pts]
    ys = [p[1] for pts in data.values() for p in pts]
    x0, x1, xstep = nice_limits(min(xs), max(xs))
    y0, y1, ystep = nice_limits(min(ys), max(ys))
    if xrange_:
        x0, x1 = xrange_
        xstep = None
    if yrange_:
        y0, y1 = yrange_
        ystep = None
    return (x0, x1, xstep), (y0, y1, ystep)


# ----------------------------------------------------------------- plotting
def plot_matplotlib(data, a, xl, yl):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    fig, ax = plt.subplots(figsize=(8, 5))
    for label, pts in data.items():
        xs, ys = zip(*pts)
        line, = ax.plot(xs, ys, marker="o", linestyle="none" if a.no_line else "-", label=label)
        if a.fit and (res := linear_fit(xs, ys)):
            m, c, r2 = res
            fx = [min(xs), max(xs)]
            ax.plot(fx, [m * x + c for x in fx], "--", color=line.get_color(), alpha=0.7,
                    label=f"{label} fit: y={m:.4g}x{c:+.4g} (R²={r2:.3f})")
            print(f"{label}: y = {m:.6g} x {c:+.6g}   R² = {r2:.4f}")

    (x0, x1, xs_), (y0, y1, ys_) = ranges(data, a.xrange, a.yrange)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    if xs_:
        ax.xaxis.set_major_locator(MultipleLocator(xs_))
    if ys_:
        ax.yaxis.set_major_locator(MultipleLocator(ys_))
    if y0 < 0 < y1:
        ax.axhline(0, color="black", linewidth=0.8)
    if x0 < 0 < x1:
        ax.axvline(0, color="black", linewidth=0.8)

    ax.set_title(a.title or f"{yl} vs {xl}")
    ax.set_xlabel(a.xlabel or xl)
    ax.set_ylabel(a.ylabel or yl)
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend()

    if a.save:
        fig.savefig(a.save, dpi=200, bbox_inches="tight")
        print(f"Saved {a.save}")
    else:
        plt.show()


def plot_terminal(data, a, xl, yl):
    try:
        import plotext as plt
    except ImportError:
        sys.exit("Terminal mode needs plotext:  pip install plotext")

    for label, pts in data.items():
        xs, ys = zip(*pts)
        plt.plot(xs, ys, marker="dot", label=label)
        if a.fit and (res := linear_fit(xs, ys)):
            m, c, r2 = res
            print(f"{label}: y = {m:.6g} x {c:+.6g}   R² = {r2:.4f}")
    (x0, x1, _), (y0, y1, _) = ranges(data, a.xrange, a.yrange)
    plt.xlim(x0, x1)
    plt.ylim(y0, y1)
    plt.title(a.title or f"{yl} vs {xl}")
    plt.xlabel(a.xlabel or xl)
    plt.ylabel(a.ylabel or yl)
    plt.grid(True, True)
    plt.show()


# ----------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Plot a CSV file as a lab graph.")
    p.add_argument("csv", help="path to the CSV file")
    p.add_argument("--x", help="x column name (default: first column)")
    p.add_argument("--y", nargs="+", help="y column name(s) (default: all other columns)")
    p.add_argument("--group", help="column whose values split the rows into separate lines")
    p.add_argument("--title")
    p.add_argument("--xlabel")
    p.add_argument("--ylabel")
    p.add_argument("--xrange", nargs=2, type=float, metavar=("MIN", "MAX"))
    p.add_argument("--yrange", nargs=2, type=float, metavar=("MIN", "MAX"))
    p.add_argument("--fit", action="store_true", help="add best-fit straight line + equation")
    p.add_argument("--no-line", action="store_true", help="points only, don't connect them")
    p.add_argument("--save", nargs="?", const="graph.png", help="save to PNG instead of showing")
    p.add_argument("--terminal", action="store_true", help="draw inside the terminal")
    a = p.parse_args()

    xcol, ycols, data = load(a.csv, a.x, a.y, a.group)
    yl = ", ".join(ycols)
    (plot_terminal if a.terminal else plot_matplotlib)(data, a, xcol, yl)


if __name__ == "__main__":
    main()

# plot.py: lab graph plotter

Turn a table of lab readings (CSV) into a clean graph with one command.
Works for any experiment, as long as you have an x column and one or more y columns.

## Setup

```bash
pip install matplotlib          # required
pip install plotext             # optional, only for --terminal mode
```

Python 3.8+ (3.8 or newer because the script uses the `:=` operator).

## Quick start

1. Put your readings in a CSV file. The first row is the column names:

   ```
   rpm,voltage
   1500,202
   1428,180
   1289,167
   ```

2. Run:

   ```bash
   python plot.py data.csv
   ```

A window opens with the graph. Points are marked and connected, the axes have neat ranges, and the grid and labels are filled in.

## CSV rules

- Row 1 is the header (column names). Data starts on row 2.
- By default the **first column is x** and **every other column is a y-series**, so several y columns give several lines on one graph.
- Blank or non-numeric cells are skipped. The script prints which CSV line numbers it skipped, so you can fix them.
- Rows don't need to be in order; the script sorts each line by x before connecting the points.
- Different series may have different x values. Use the `--group` column for that (see below).

## Options

| Flag | What it does |
|---|---|
| `--x NAME` | Column to use for x (default: first column) |
| `--y NAME [NAME ...]` | Column(s) to use for y (default: all other columns) |
| `--group NAME` | Split rows into separate lines by the values in this column |
| `--title "..."` | Graph title (default: "y vs x") |
| `--xlabel "..."`, `--ylabel "..."` | Axis labels (default: the column names) |
| `--xrange MIN MAX`, `--yrange MIN MAX` | Set the axis range yourself |
| `--fit` | Add a best-fit straight line, and print its equation and R² |
| `--no-line` | Points only, don't connect them |
| `--save [FILE]` | Save as PNG instead of opening a window (default `graph.png`) |
| `--terminal` | Draw the graph inside the terminal (needs `plotext`) |

## Examples

```bash
# Simplest case
python plot.py data.csv

# Pick columns, add labels, save a PNG
python plot.py data.csv --x I_mA --y V_volts --title "V-I characteristic" \
    --xlabel "Current (mA)" --ylabel "Voltage (V)" --save vi.png

# Two curves with different x values (see "Grouping" below)
python plot.py motor.csv --x rpm --y voltage --group direction --fit

# Scatter plot with a best-fit line, in the terminal
python plot.py data.csv --no-line --fit --terminal

# Choose the range manually
python plot.py data.csv --xrange 0 100 --yrange 0 50
```

## Grouping: several lines with different x values

When each line has its own x values (like the +ve and -ve tables in the DC motor experiment), put everything in one CSV with an extra column saying which line each row belongs to:

```
direction,rpm,voltage
+ve,1500,202
+ve,1428,180
-ve,1571,-204
-ve,1475,-192
```

Then use `--group direction`. Each distinct value in that column (`+ve`, `-ve`) becomes its own line with its own legend entry. `motor.csv` in this folder is a working example.

## How it works

The script is a simple pipeline: **load, choose ranges, plot**.

1. **Load** (`load`)
   - Reads the CSV with Python's `csv` module.
   - Works out which column is x, which are y, and (optionally) which is the group column.
   - Converts each x/y pair to numbers. Anything unreadable is skipped and reported.
   - Collects the points into named series and sorts each series by x so the connecting line doesn't zigzag.

2. **Choose ranges** (`nice_limits`, `ranges`)
   - Finds the smallest and largest value on each axis across all series.
   - Aims for about 6 tick marks, picks a step size of 1, 2 or 5 times a power of ten (like 50, 100 or 200), and rounds the axis limits outward to multiples of that step.
   - So data from 1062 to 1571 becomes an axis of 1000 to 1600 in steps of 100.
   - If you pass `--xrange` or `--yrange`, your values override the automatic ones.
   - If the axis range crosses zero, a darker zero line is drawn.

3. **Plot** (`plot_matplotlib` or `plot_terminal`)
   - Draws one line per series with round markers, plus the grid, title, labels and legend.
   - With `--fit`, it also draws a dashed line of the same colour as its series.

### The best-fit line (`linear_fit`)

It uses ordinary least squares for a straight line `y = m·x + c`:

- slope `m = Σ(x − x̄)(y − ȳ) / Σ(x − x̄)²`
- intercept `c = ȳ − m·x̄`
- `R² = 1 − SS_res / SS_tot`, where 1.0 means the points sit exactly on the line

The equation and R² are shown in the legend and printed in the terminal. A low R² means the data doesn't follow a straight line well. That can be a real nonlinear effect, or a mistaken reading worth checking.

### Terminal mode

`--terminal` uses the `plotext` library to draw the same data as text characters in your terminal. This is useful over SSH or when you don't want a window. It is lower resolution than matplotlib, so use `--save` for anything you will submit.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Column 'x' not found` | The name must match the CSV header exactly (capitals count). The error lists the real column names. |
| `Skipped unreadable/blank values on CSV line(s): [...]` | Those cells are empty or contain text. Fix them in the CSV. Line 1 is the header. |
| Graph looks squashed into a corner | Use `--xrange` and `--yrange` to zoom in on your data. |
| No window opens (e.g. over SSH) | Use `--save` or `--terminal`. |
| `Terminal mode needs plotext` | Run `pip install plotext`. |
| Wrong column is used as x | Add `--x yourcolumn`. |

## Files

- `plot.py`: the program
- `motor.csv`: example data (DC motor, voltage vs speed, +ve and -ve)
- `README.md`: this file

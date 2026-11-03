#!/usr/bin/env python3
"""
spectrum.py - terminal spectrum analyzer for whatever your laptop is playing.

Captures the default output's monitor source via `parec` (works on both
PulseAudio and PipeWire-pulse), runs a windowed FFT, groups bins into
log-spaced bands, and draws smoothed bars with peak caps in truecolor
using 1/8-cell block characters.

Deps:   sudo pacman -S libpulse python-numpy   (parec + numpy)
Run:    python spectrum.py
        python spectrum.py --theme fire --width 1 --gain 6
Quit:   Ctrl-C
"""
import argparse
import colorsys
import shutil
import signal
import subprocess
import sys
import threading
import time

import numpy as np

RATE = 44100
N = 4096                    # FFT size (~93 ms window, ~10 Hz bin resolution)
HOP = RATE // 60            # samples pulled per ~16 ms
FMIN, FMAX = 35.0, 16000.0
DB_FLOOR, DB_CEIL = -78.0, -18.0
TILT_DB_PER_OCT = 3.0       # compensates the natural 1/f roll-off of music

LEVELS = np.array([" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"])
CAP = "▔"

# (hue_bottom, hue_top, saturation, value)
THEMES = {
    "neon":   (0.52, 0.93, 0.85, 1.00),   # cyan -> magenta
    "fire":   (0.02, 0.15, 0.90, 1.00),   # red -> yellow
    "matrix": (0.38, 0.30, 0.80, 1.00),   # green
    "ice":    (0.62, 0.50, 0.55, 1.00),   # blue -> aqua
    "sunset": (0.75, 0.05, 0.80, 1.00),   # purple -> orange
}


class Capture(threading.Thread):
    """Reads raw s16le stereo from parec and keeps the latest N mono samples."""

    def __init__(self, source: str):
        super().__init__(daemon=True)
        self.proc = subprocess.Popen(
            ["parec", "-d", source, "--format=s16le", f"--rate={RATE}",
             "--channels=2", "--latency-msec=20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.buf = np.zeros(N, dtype=np.float32)
        self.lock = threading.Lock()

    def run(self):
        chunk = HOP * 4  # 2 channels * 2 bytes
        while True:
            data = self.proc.stdout.read(chunk)
            if not data:
                break
            data = data[: len(data) // 4 * 4]
            x = np.frombuffer(data, dtype=np.int16).reshape(-1, 2)
            mono = x.astype(np.float32).mean(axis=1) / 32768.0
            mono = mono[-N:]
            with self.lock:
                self.buf = np.concatenate((self.buf[len(mono):], mono))

    def snapshot(self) -> np.ndarray:
        with self.lock:
            return self.buf.copy()

    def alive(self) -> bool:
        return self.proc.poll() is None


class Bands:
    """Log-spaced FFT band layout for a given bar count."""

    def __init__(self, n_bars: int, span: float):
        self.span = span
        freqs = np.fft.rfftfreq(N, 1.0 / RATE)
        edges = np.geomspace(FMIN, FMAX, n_bars + 1)
        self.lo = np.searchsorted(freqs, edges[:-1])
        self.hi = np.maximum(np.searchsorted(freqs, edges[1:]), self.lo + 1)
        centers = np.sqrt(edges[:-1] * edges[1:])
        self.tilt = TILT_DB_PER_OCT * np.log2(centers / 1000.0)
        self.n = n_bars

    def levels(self, mag_db_src: np.ndarray, gain_db: float) -> np.ndarray:
        raw = np.array([mag_db_src[l:h].max() for l, h in zip(self.lo, self.hi)])
        db = 20.0 * np.log10(raw + 1e-9) + self.tilt + gain_db
        lvl = np.clip((db - DB_CEIL + self.span) / self.span, 0.0, 1.0)
        padded = np.concatenate(([lvl[0]], lvl, [lvl[-1]]))
        return 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]


def row_colors(theme: str, rows: int):
    h0, h1, s, v = THEMES[theme]
    out = []
    for r in range(rows):
        t = r / max(1, rows - 1)
        rgb = colorsys.hsv_to_rgb(h0 + (h1 - h0) * t, s, v)
        R, G, B = (int(c * 255) for c in rgb)
        out.append(f"\x1b[38;2;{R};{G};{B}m")
    return out


def render(bars, peaks, rows, colors, bw, gap, cols):
    eighths = (bars * rows * 8).astype(int)
    peak_rows = np.minimum((peaks * rows).astype(int), rows - 1)
    pad = " " * max(0, (cols - len(bars) * (bw + gap) + gap) // 2)
    spacer = " " * gap
    cap_color = "\x1b[38;2;255;255;255m"
    lines = []
    for row in range(rows - 1, -1, -1):
        vals = np.clip(eighths - row * 8, 0, 8)
        chars = LEVELS[vals]
        caps = (peak_rows == row) & (vals == 0) & (peaks > 0.02)
        parts = []
        for ch, is_cap in zip(chars, caps):
            if is_cap:
                parts.append(cap_color + CAP * bw + colors[row] + spacer)
            else:
                parts.append(ch * bw + spacer)
        lines.append(pad + colors[row] + "".join(parts) + "\x1b[0m")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="@DEFAULT_MONITOR@",
                    help="pulse source to capture (default: monitor of default output)")
    ap.add_argument("--theme", choices=THEMES, default="neon")
    ap.add_argument("--width", type=int, default=2, help="bar width in columns")
    ap.add_argument("--gap", type=int, default=1, help="gap between bars")
    ap.add_argument("--gain", type=float, default=0.0, help="extra gain in dB")
    ap.add_argument("--range", type=float, default=45.0,
                    help="dB span shown, ending at the ceiling (smaller = more contrast)")
    ap.add_argument("--decay", type=float, default=0.92,
                    help="bar fall-off per frame, 0-1 (lower = snappier, higher = smoother)")
    ap.add_argument("--fps", type=int, default=60)
    args = ap.parse_args()

    if shutil.which("parec") is None:
        sys.exit("parec not found. Install it with: sudo pacman -S libpulse")

    cap = Capture(args.source)
    cap.start()
    time.sleep(0.15)
    if not cap.alive():
        sys.exit("parec exited immediately. Check sources with: pactl list short sources")

    window = np.hanning(N).astype(np.float32)
    norm = N / 4.0  # amplitude normalisation for a Hann-windowed sine

    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l\x1b[2J")  # alt screen, hide cursor, clear
    out.flush()

    def cleanup(*_):
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        cap.proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    last_size = None
    bands = None
    colors = None
    bars = peaks = peak_vel = None
    frame_dt = 1.0 / args.fps

    while True:
        t0 = time.perf_counter()
        cols, lines = shutil.get_terminal_size()
        rows = max(4, lines)
        n_bars = max(8, cols // (args.width + args.gap))

        if (cols, lines) != last_size:
            last_size = (cols, lines)
            bands = Bands(n_bars, args.range)
            colors = row_colors(args.theme, rows)
            bars = np.zeros(n_bars)
            peaks = np.zeros(n_bars)
            peak_vel = np.zeros(n_bars)
            out.write("\x1b[2J")

        if not cap.alive():
            cleanup()

        spec = np.abs(np.fft.rfft(cap.snapshot() * window)) / norm
        target = bands.levels(spec, args.gain)

        # fast attack, slow decay
        rise = target > bars
        bars = np.where(rise, bars + (target - bars) * 0.5,
                        bars * args.decay + target * (1.0 - args.decay))

        # peak caps: hold briefly, then fall with gravity
        hit = bars >= peaks
        peaks = np.where(hit, bars, peaks)
        peak_vel = np.where(hit, 0.0, peak_vel + 0.0009)
        peaks = np.maximum(peaks - peak_vel, bars)

        frame = render(bars, peaks, rows, colors, args.width, args.gap, cols)
        out.write("\x1b[H" + frame)
        out.flush()

        sleep = frame_dt - (time.perf_counter() - t0)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()

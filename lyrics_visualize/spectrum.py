#!/usr/bin/env python3
"""
spectrum.py - terminal spectrum analyzer + synced lyrics for whatever your
laptop is playing.

Audio:   `parec` captures the default output's monitor source (PulseAudio or
         PipeWire-pulse), then a windowed FFT -> log-spaced bands -> smoothed
         bars with peak caps, drawn in truecolor with 1/8-cell blocks.
Lyrics:  `playerctl` reads the now-playing track + position over MPRIS (the
         same source your GNOME media extension uses), and synced lyrics are
         fetched from LRCLIB (https://lrclib.net, free, no API key).

Deps:   sudo pacman -S libpulse python-numpy playerctl
Run:    python spectrum.py
        python spectrum.py --theme sunset --gain -6
        python spectrum.py --player firefox --lyric-offset 0.3
        python spectrum.py --no-lyrics
Keys:   , .  jump to previous / next lyric line (use when lyrics are out of sync)
        [ ]  nudge lyrics 0.5 s later / earlier        0  reset
Quit:   Ctrl-C
"""
import argparse
import bisect
import colorsys
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

RATE = 44100
N = 4096                    # FFT size (~93 ms window, ~10 Hz bin resolution)
HOP = RATE // 60            # samples pulled per ~16 ms
FMIN, FMAX = 35.0, 16000.0
DB_CEIL = -18.0
TILT_DB_PER_OCT = 3.0       # compensates the natural 1/f roll-off of music

LEVELS = np.array([" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"])
CAP = "▔"

LYRIC_ROWS = 8              # header + 5 lyric lines + spacing, when lyrics are on
LRCLIB = "https://lrclib.net/api"
UA = "spectrum.py/1.0 (terminal visualizer)"

# (hue_bottom, hue_top, saturation, value)
THEMES = {
    "neon":   (0.52, 0.93, 0.85, 1.00),   # cyan -> magenta
    "fire":   (0.02, 0.15, 0.90, 1.00),   # red -> yellow
    "matrix": (0.38, 0.30, 0.80, 1.00),   # green
    "ice":    (0.62, 0.50, 0.55, 1.00),   # blue -> aqua
    "sunset": (0.75, 0.05, 0.80, 1.00),   # purple -> orange
}


# --------------------------------------------------------------------------
# Audio capture
# --------------------------------------------------------------------------
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

    def levels(self, mag: np.ndarray, gain_db: float) -> np.ndarray:
        raw = np.array([mag[l:h].max() for l, h in zip(self.lo, self.hi)])
        db = 20.0 * np.log10(raw + 1e-9) + self.tilt + gain_db
        lvl = np.clip((db - DB_CEIL + self.span) / self.span, 0.0, 1.0)
        padded = np.concatenate(([lvl[0]], lvl, [lvl[-1]]))
        return 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]


# --------------------------------------------------------------------------
# Now-playing (MPRIS via playerctl)
# --------------------------------------------------------------------------
SEP = "\x1f"


class NowPlaying(threading.Thread):
    """Polls playerctl for artist/title/length/position/status (~5 Hz).

    Browsers (Firefox especially) often report a stale or zero MPRIS position,
    so we keep our own clock: it free-runs while playing and re-syncs whenever
    the reported position actually changes (seeks, or players that report it).
    """

    def __init__(self, player: str | None):
        super().__init__(daemon=True)
        fmt = SEP.join(["{{artist}}", "{{title}}", "{{album}}",
                        "{{mpris:length}}", "{{status}}"])
        base = ["playerctl"] + (["-p", player] if player else [])
        self.meta_cmd = base + ["metadata", "--format", fmt]
        self.pos_cmd = base + ["position"]
        self.lock = threading.Lock()
        self.artist = self.title = self.album = ""
        self.length = 0.0          # seconds
        self._clock = 0.0          # best-guess position (s) at _stamp
        self._stamp = time.monotonic()
        self._key = None
        self._last_reported = None
        self._changed_at = time.monotonic()
        self.playing = False
        self.available = False
        self.estimated = False     # True when position isn't coming from the player

    def _run(self, cmd):
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=2).stdout.strip()

    def run(self):
        while True:
            try:
                parts = self._run(self.meta_cmd).split(SEP)
                if len(parts) == 5:
                    artist, title, album, length, status = parts
                    try:
                        reported = float(self._run(self.pos_cmd))
                    except ValueError:
                        reported = None
                    now = time.monotonic()
                    with self.lock:
                        key = (artist, title)
                        if key != self._key:                 # new track
                            self._key = key
                            self._clock = reported or 0.0
                            self._last_reported = reported
                            self._changed_at = now
                            self.estimated = False
                        else:
                            if self.playing:                 # advance over the last interval
                                self._clock += now - self._stamp
                            if reported is not None and reported != self._last_reported:
                                self._clock = reported       # player gave a fresh value
                                self._changed_at = now
                                self.estimated = False
                            elif self.playing and now - self._changed_at > 2.0:
                                self.estimated = True
                            self._last_reported = reported
                        self._stamp = now
                        self.artist, self.title, self.album = artist, title, album
                        self.length = _num(length) / 1e6
                        self.playing = status == "Playing"
                        self.available = True
                else:
                    with self.lock:
                        self.available = False
                        self.playing = False
            except (subprocess.SubprocessError, OSError):
                with self.lock:
                    self.available = False
            time.sleep(0.2)

    def state(self):
        """(available, artist, title, album, length_s, position_s, playing, estimated)"""
        with self.lock:
            pos = self._clock
            if self.playing:
                pos += time.monotonic() - self._stamp
            return (self.available, self.artist, self.title, self.album,
                    self.length, pos, self.playing, self.estimated)


def _num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# Lyrics (LRCLIB)
# --------------------------------------------------------------------------
_JUNK = re.compile(
    r"[\(\[][^\)\]]*(official|video|lyric|audio|visuali[sz]er|hd|4k|remaster|"
    r"explicit|mv|m/v)[^\)\]]*[\)\]]", re.I)
_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_LRC_LINE = re.compile(r"^\s*((?:\[\d+:\d+(?:[.:]\d+)?\])+)\s*(.*)$")
_LRC_TS = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")


def clean_artist(a: str) -> str:
    a = re.sub(r"\s*-\s*Topic$", "", a, flags=re.I)
    a = re.sub(r"VEVO$", "", a, flags=re.I)
    return a.strip()


def title_candidates(artist: str, title: str):
    """Most specific -> most generic title guesses (YouTube titles are messy)."""
    t = title.strip()
    # "Artist - Song" embedded in the title
    if " - " in t and (not artist or artist.lower() in t.lower()):
        left, right = t.split(" - ", 1)
        if not artist:
            artist = left.strip()
        t = right.strip()
    cands = [t, _JUNK.sub("", t).strip(), _PARENS.sub("", t).strip()]
    seen, out = set(), []
    for c in cands:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return artist, out


def parse_lrc(text: str):
    """-> sorted list of (seconds, line)."""
    rows = []
    for raw in text.splitlines():
        m = _LRC_LINE.match(raw)
        if not m:
            continue
        body = m.group(2).strip()
        for mm, ss, frac in _LRC_TS.findall(m.group(1)):
            f = float("0." + frac) if frac else 0.0
            rows.append((int(mm) * 60 + int(ss) + f, body))
    rows.sort(key=lambda r: r[0])
    return rows


def _get(path: str, params: dict):
    url = f"{LRCLIB}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_lyrics(artist: str, title: str, album: str, duration: float):
    """Returns (status, rows). status: 'synced' | 'plain' | 'instrumental' | 'none'."""
    artist = clean_artist(artist)
    artist, titles = title_candidates(artist, title)
    dur = int(round(duration)) if duration else None

    def pick(rec):
        if not rec:
            return None
        if rec.get("instrumental"):
            return ("instrumental", [])
        if rec.get("syncedLyrics"):
            rows = parse_lrc(rec["syncedLyrics"])
            if rows:
                return ("synced", rows)
        if rec.get("plainLyrics"):
            return ("plain", [(0.0, l) for l in rec["plainLyrics"].splitlines()])
        return None

    # 1) exact-ish lookups
    for t in titles:
        params = {"track_name": t, "artist_name": artist}
        if album:
            params["album_name"] = album
        if dur:
            params["duration"] = dur
        for p in (params, {k: v for k, v in params.items() if k != "album_name"}):
            try:
                res = pick(_get("get", p))
                if res:
                    return res
            except urllib.error.HTTPError:
                pass          # 404 = not found, try next
    # 2) fuzzy search, choose the closest duration that has synced lyrics
    for t in titles:
        try:
            hits = _get("search", {"track_name": t, "artist_name": artist})
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if not isinstance(hits, list) or not hits:
            try:
                hits = _get("search", {"q": f"{artist} {t}".strip()})
            except (urllib.error.URLError, OSError, ValueError):
                continue
        if not isinstance(hits, list):
            continue
        hits = [h for h in hits if h.get("syncedLyrics") or h.get("plainLyrics")]
        if dur:
            hits.sort(key=lambda h: abs((h.get("duration") or 0) - dur))
        if hits:
            res = pick(hits[0])
            if res:
                return res
    return ("none", [])


class Lyrics:
    """Tracks the current song, fetches lyrics in the background, caches them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.status = "idle"       # idle | loading | synced | plain | instrumental | none | error
        self.rows = []
        self.times = []
        self._cache = {}

    def update(self, artist, title, album, length):
        key = (artist, title)
        if not title or key == self.key:
            return
        self.key = key
        if key in self._cache:
            self._set(*self._cache[key])
            return
        self._set("loading", [])
        threading.Thread(target=self._work, args=(key, artist, title, album, length),
                         daemon=True).start()

    def _work(self, key, artist, title, album, length):
        try:
            res = fetch_lyrics(artist, title, album, length)
        except Exception:
            res = ("error", [])
        if res[0] != "error":
            self._cache[key] = res
        if self.key == key:        # ignore stale results after a skip
            self._set(*res)

    def _set(self, status, rows):
        with self.lock:
            self.status, self.rows = status, rows
            self.times = [t for t, _ in rows]

    def jump(self, pos: float, direction: int):
        """Seconds to add to the offset so the next/previous lyric line becomes current."""
        with self.lock:
            if self.status != "synced" or not self.times:
                return 0.0
            idx = bisect.bisect_right(self.times, pos) - 1
            tgt = min(max(idx + direction, 0), len(self.times) - 1)
            return self.times[tgt] - pos + 0.02

    def window(self, pos: float, half: int):
        """-> (status, [(offset, text)] for offsets -half..+half, where 0 = current)."""
        with self.lock:
            status, rows, times = self.status, self.rows, self.times
            if status != "synced":
                return status, None
            idx = bisect.bisect_right(times, pos) - 1
            out = []
            for off in range(-half, half + 1):
                i = idx + off
                text = rows[i][1] if 0 <= i < len(rows) else ""
                out.append((off, text))
            return status, out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def theme_rgb(theme: str, t: float):
    h0, h1, s, v = THEMES[theme]
    rgb = colorsys.hsv_to_rgb(h0 + (h1 - h0) * t, s, v)
    return tuple(int(c * 255) for c in rgb)


def fg(rgb):
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def row_colors(theme: str, rows: int):
    return [fg(theme_rgb(theme, r / max(1, rows - 1))) for r in range(rows)]


def center(text: str, cols: int) -> str:
    if len(text) > cols - 2:
        text = text[: max(0, cols - 3)] + "…"
    return " " * max(0, (cols - len(text)) // 2) + text


def render_lyrics(np_state, lyr: Lyrics, theme: str, cols: int, offset: float):
    """Fixed LYRIC_ROWS lines: header, blank, 5 lyric lines, blank."""
    avail, artist, title, _album, _length, pos, playing, estimated = np_state
    accent = theme_rgb(theme, 0.85)
    dim = "\x1b[38;2;120;120;135m"
    K = "\x1b[K"
    lines = []

    if avail and title:
        head = f"{clean_artist(artist)} — {title}" if artist else title
        if not playing:
            head += "  ⏸"
    else:
        head = "nothing playing"
    lines.append(fg(accent) + "\x1b[1m" + center(head, cols) + "\x1b[0m" + K)
    if estimated:
        hint = "position estimated  ·  , . jump a line  ·  [ ] nudge 0.5s  ·  0 reset"
        lines.append(dim + center(hint, cols) + "\x1b[0m" + K)
    else:
        lines.append(K)

    body = [""] * 5
    msg = None
    if not (avail and title):
        msg = "start some music…"
    else:
        lyr.update(artist, title, _album, _length)
        status, win = lyr.window(pos + offset, 2)
        if status == "loading":
            msg = "fetching lyrics…"
        elif status == "none":
            msg = "no lyrics found"
        elif status == "error":
            msg = "couldn't reach lyrics service"
        elif status == "instrumental":
            msg = "♪ instrumental ♪"
        elif status == "plain":
            msg = "lyrics available but not time-synced"
        elif win:
            body = [t for _, t in win]

    if msg:
        body[2] = msg
        shades = [dim] * 5
        styles = ["", "", "\x1b[3m", "", ""]
    else:
        shades = [
            "\x1b[38;2;70;70;85m",
            "\x1b[38;2;115;115;135m",
            "\x1b[1m" + "\x1b[38;2;255;255;255m",
            "\x1b[38;2;115;115;135m",
            "\x1b[38;2;70;70;85m",
        ]
        styles = [""] * 5

    for i in range(5):
        lines.append(shades[i] + styles[i] + center(body[i], cols) + "\x1b[0m" + K)
    lines.append(K)
    return "\n".join(lines)


def render_bars(bars, peaks, rows, colors, bw, gap, cols):
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


# --------------------------------------------------------------------------
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
    ap.add_argument("--no-lyrics", action="store_true", help="bars only")
    ap.add_argument("--player", default=None,
                    help="playerctl player name, e.g. firefox, spotify (default: first active)")
    ap.add_argument("--lyric-offset", type=float, default=0.0,
                    help="seconds to shift lyrics (+ = show lines earlier)")
    args = ap.parse_args()

    if shutil.which("parec") is None:
        sys.exit("parec not found. Install it with: sudo pacman -S libpulse")
    use_lyrics = not args.no_lyrics
    if use_lyrics and shutil.which("playerctl") is None:
        print("playerctl not found, running without lyrics "
              "(sudo pacman -S playerctl)", file=sys.stderr)
        use_lyrics = False

    cap = Capture(args.source)
    cap.start()
    time.sleep(0.15)
    if not cap.alive():
        sys.exit("parec exited immediately. Check sources with: pactl list short sources")

    nowp = lyr = None
    if use_lyrics:
        nowp = NowPlaying(args.player)
        nowp.start()
        lyr = Lyrics()

    fd = sys.stdin.fileno()
    old_tty = termios.tcgetattr(fd) if sys.stdin.isatty() else None
    if old_tty is not None:
        tty.setcbreak(fd)            # keys without Enter; Ctrl-C still works
    lyric_offset = args.lyric_offset

    window = np.hanning(N).astype(np.float32)
    norm = N / 4.0  # amplitude normalisation for a Hann-windowed sine

    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l\x1b[2J")  # alt screen, hide cursor, clear
    out.flush()

    def cleanup(*_):
        if old_tty is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
        out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        cap.proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    last_size = None
    bands = colors = None
    bars = peaks = peak_vel = None
    top = 0
    frame_dt = 1.0 / args.fps

    while True:
        t0 = time.perf_counter()
        cols, lines = shutil.get_terminal_size()
        top = LYRIC_ROWS if (use_lyrics and lines >= LYRIC_ROWS + 8) else 0
        rows = max(4, lines - top)
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

        # soft attack, slow decay
        rise = target > bars
        bars = np.where(rise, bars + (target - bars) * 0.5,
                        bars * args.decay + target * (1.0 - args.decay))

        # peak caps: hold briefly, then fall with gravity
        hit = bars >= peaks
        peaks = np.where(hit, bars, peaks)
        peak_vel = np.where(hit, 0.0, peak_vel + 0.0009)
        peaks = np.maximum(peaks - peak_vel, bars)

        frame = render_bars(bars, peaks, rows, colors, args.width, args.gap, cols)
        if top:
            if old_tty is not None:
                while select.select([fd], [], [], 0)[0]:
                    for ch in os.read(fd, 32).decode(errors="ignore"):
                        if ch == "[":
                            lyric_offset -= 0.5
                        elif ch == "]":
                            lyric_offset += 0.5
                        elif ch == "0":
                            lyric_offset = args.lyric_offset
                        elif ch in ",.":
                            p = nowp.state()[5] + lyric_offset
                            lyric_offset += lyr.jump(p, 1 if ch == "." else -1)
            head = render_lyrics(nowp.state(), lyr, args.theme, cols, lyric_offset)
            frame = head + "\n" + frame
        out.write("\x1b[H" + frame)
        out.flush()

        sleep = frame_dt - (time.perf_counter() - t0)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()

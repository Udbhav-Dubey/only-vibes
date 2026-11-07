#!/usr/bin/env python3
"""desk-buddy server (stdlib only). Hooks POST /event, the phone listens on /events (SSE)."""
import bisect, glob, json, os, queue, re, select, shlex, shutil, struct, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 3030))
HERE = os.path.dirname(os.path.abspath(__file__))

lock = threading.Lock()
clients = set()                       # one queue per connected phone
state = {"mode": "idle", "text": "", "since": int(time.time() * 1000)}
screen = {"w": 1920, "h": 1080}
last_move = time.time()
last_lyric = 0.0
last_pos = (-1, -1)
nvim_up = False
dsa_auto = False
lyric_offset = float(os.environ.get("BUDDY_LYRIC_OFFSET", "0"))
last_track = None
dsa_started = 0.0
dsa_history_path = os.path.join(HERE, "dsa_history.json")
pomo = None
pomo_lock = threading.Lock()
keys = deque(maxlen=300)              # key-press TIMESTAMPS only (never which key), for typing speed
fail_streak = 0
music = {"on": False, "name": "", "line": "", "status": ""}   # lyrics layer: independent of the mode
SIT_MIN = float(os.environ.get("BUDDY_SIT_MIN", "90"))   # minutes of continuous activity before a stretch nudge


def send(ev):
    with lock:
        for q in clients:
            q.put(ev)


def snap():
    with lock:
        s = dict(state)
    if "until" in s:
        s["left"] = max(0, int(s["until"] - time.time() * 1000))
    return s


def set_mode(mode, text="", **extra):
    global state
    with lock:
        state = {"mode": mode, "text": text, "since": int(time.time() * 1000), **extra}
    send({"type": "state", "state": snap()})


def handle(ev):
    global last_lyric, pomo
    t = ev.get("type")
    if t == "pomo":
        return pomo_cmd(ev)
    if t == "cmd":
        return on_cmd(ev)
    if t == "mode":
        with pomo_lock:
            pomo = None
        return set_mode(ev.get("value") or "idle", ev.get("text") or "")
    if t == "lyric":
        last_lyric = time.time()
        music.update(on=True, line=ev.get("line") or "")      # lyrics never change the mode
    elif t == "dsa_start":
        global dsa_started
        dsa_started = time.time()
        set_mode("dsa", ev.get("text") or "today's problem")
    elif t == "dsa_solved":
        record_dsa(ev.get("text") or state.get("text") or "Solved")
        send({"type": "flash", "mood": "overjoyed"})
    elif t == "build_pass":
        send({"type": "flash", "mood": "overjoyed" if int(ev.get("streak", 0)) >= 3 else "happy"})
    elif t == "build_failed":
        send({"type": "flash", "mood": "worry" if int(ev.get("streak", 1)) >= 3 else "flinch"}) 
    elif t == "git_commit":
        send({"type": "flash", "mood": "happy", "text": "nice"})
    elif t == "git_force_push":
        send({"type": "flash", "mood": "panic"})
    elif t == "terminal_error":
        send({"type": "flash", "mood": "flinch"})
    elif t in ("pomodoro_focus", "pomodoro_break", "cpu", "battery", "network", "typing"):
        pass
    send(ev)  # all events are broadcast to connected Buddy pages



# ---- server-side Pomodoro ----
def pomo_phase(phase, minutes):
    pomo["phase"] = phase
    pomo["until"] = time.time() + minutes * 60
    done = 4 if phase == "long" else pomo["n"] % 4
    set_mode("pomo", phase, phase=phase, until=int(pomo["until"] * 1000), done=done)


def pomo_next(counted):
    if pomo["phase"] == "focus":
        if counted:
            pomo["n"] += 1
        brk = "long" if counted and pomo["n"] % 4 == 0 else "short"
        pomo_phase(brk, pomo[brk])
        send({"type": "flash", "mood": "happy"})
    else:
        pomo_phase("focus", pomo["focus"])


def pomo_cmd(ev):
    global pomo
    action = ev.get("action", "start")
    with pomo_lock:
        if action == "start":
            def mins(key, default):
                try: return min(240.0, max(0.1, float(ev.get(key, default))))
                except (TypeError, ValueError): return default
            pomo = {"n": 0, "focus": mins("focus", 25), "short": mins("short", 5), "long": mins("long", 15)}
            pomo_phase("focus", pomo["focus"])
        elif action == "skip" and pomo:
            pomo_next(counted=False)
        elif action == "stop" and pomo:
            pomo = None
            set_mode("coding" if nvim_up else "idle")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---- now-playing + LRCLIB lyrics (reused from spectrum.py; audio FFT omitted) ----
SEP = "\x1f"
LRCLIB = "https://lrclib.net/api"
UA = "spectrum.py/1.0 (terminal visualizer)"
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

    def window_ids(self, pos: float, before: int, after: int):
        """Return status, current lyric index, and indexed lines around playback position."""
        with self.lock:
            if self.status != "synced" or not self.times:
                return self.status, 0, []
            idx = bisect.bisect_right(self.times, pos) - 1
            out = [(i, self.rows[i][1])
                   for i in range(idx - before, idx + after + 1)
                   if 0 <= i < len(self.rows)]
            return "synced", idx, out

    def jump(self, pos: float, direction: int):
        """Seconds to add to the offset so the next/previous lyric line becomes current."""
        with self.lock:
            if self.status != "synced" or not self.times:
                return 0.0
            idx = bisect.bisect_right(self.times, pos) - 1
            tgt = min(max(idx + direction, 0), len(self.times) - 1)
            return self.times[tgt] - pos + 0.02



def record_dsa(problem):
    try:
        data = json.load(open(dsa_history_path))
    except (OSError, ValueError):
        data = {"streak": 0, "solved": []}
    today = time.strftime("%Y-%m-%d")
    if not any(x.get("date") == today and x.get("problem") == problem for x in data.get("solved", [])):
        data.setdefault("solved", []).append({"date": today, "problem": problem})
        data["streak"] = int(data.get("streak", 0)) + 1
    with open(dsa_history_path, "w") as f:
        json.dump(data, f, indent=2)
    send({"type": "dsa_history", "history": data})

# ---- machine watchers ----
# X11 session: xdotool gives the exact cursor position.
# Wayland session: apps can't read the cursor, so we read /dev/input directly and keep a
# virtual cursor (relative mouse motion + touchpad deltas). Good enough for "look where I moved".
# The input events are only used for movement/activity timing; nothing is stored or logged.
SESSION = os.environ.get("XDG_SESSION_TYPE", "")
INPUT_GLOB = os.environ.get("INPUT_GLOB", "/dev/input/event*")
EV = struct.Struct("llHHi")  # struct input_event on 64-bit Linux


def xdotool_loop():
    global last_pos, last_move
    r = run(["xdotool", "getdisplaygeometry"])
    if r is None or r.returncode:
        print("xdotool not found: no cursor tracking (sudo pacman -S xdotool)")
        return
    w, h = map(int, r.stdout.split())
    screen.update(w=w, h=h)
    while True:
        r = run(["xdotool", "getmouselocation", "--shell"])
        if r and r.returncode == 0:
            x = int(re.search(r"X=(\d+)", r.stdout).group(1))
            y = int(re.search(r"Y=(\d+)", r.stdout).group(1))
            if (x, y) != last_pos:
                last_pos, last_move = (x, y), time.time()
                send({"type": "mouse", "x": x / screen["w"], "y": y / screen["h"]})
        time.sleep(0.06)


class Pointer:
    """Virtual cursor in 0..1 built from raw input events. feed() returns True if it moved."""
    def __init__(self):
        self.x = self.y = 0.5

    def feed(self, prev, t, c, v):
        global last_move
        if t == 1 and v in (1, 2):            # key / button press
            last_move = time.time()
            if v == 1 and c < 0x100:
                keys.append(last_move)
        elif t == 2:                          # EV_REL: mouse
            last_move = time.time()
            if c == 0:
                self.x += v / 1920
            elif c == 1:
                self.y += v / 1080
            else:
                return False
            self.x = min(1.0, max(0.0, self.x)); self.y = min(1.0, max(0.0, self.y))
            return True
        elif t == 3 and c in (0x35, 0x36):    # EV_ABS multitouch position: touchpad
            last_move = time.time()
            if prev.get(c) is not None:
                d = v - prev[c]
                if c == 0x35:
                    self.x += d / 1500
                else:
                    self.y += d / 900
                self.x = min(1.0, max(0.0, self.x)); self.y = min(1.0, max(0.0, self.y))
                prev[c] = v
                return True
            prev[c] = v
        elif t == 3 and c == 0x39 and v == -1:  # finger lifted: forget last touch position
            prev.clear()
        return False


def input_loop(emit_gaze):
    fds = {}
    for p in glob.glob(INPUT_GLOB):
        try:
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = {}
        except OSError:
            pass
    if not fds:
        print("can't read /dev/input: run with sudo, or: sudo usermod -aG input $USER (then log out/in)")
        return
    print(f"reading {len(fds)} input devices")
    ptr, dirty, sent = Pointer(), False, 0.0
    while True:
        ready, _, _ = select.select(list(fds), [], [], 0.05)
        for fd in ready:
            try:
                data = os.read(fd, EV.size * 64)
            except BlockingIOError:
                continue
            except OSError:                    # device unplugged
                os.close(fd); del fds[fd]; continue
            for off in range(0, len(data) - EV.size + 1, EV.size):
                _, _, t, c, v = EV.unpack_from(data, off)
                dirty |= ptr.feed(fds[fd], t, c, v)
        if emit_gaze and dirty and time.time() - sent > 0.03:
            send({"type": "mouse", "x": ptr.x, "y": ptr.y})
            dirty, sent = False, time.time()
        if not fds:
            return


def dsa_session_up():
    r = run(["tmux", "list-sessions", "-F", "#{session_name} #{session_attached}"])
    if not r or r.returncode:
        return False
    for line in r.stdout.splitlines():
        name, _, n = line.rpartition(" ")
        if name == "dsa" and n.isdigit() and int(n) > 0:
            return True
    return False


def tick_loop():
    global nvim_up, dsa_auto
    while True:
        r = run(["xprintidle"])
        ms = int(r.stdout) if r and r.returncode == 0 else int((time.time() - last_move) * 1000)
        send({"type": "idle", "ms": ms})
        r = run(["pgrep", "-x", "nvim"])
        nvim_up = bool(r and r.returncode == 0)
        dsa_up = dsa_session_up()
        mode = state["mode"]
        with pomo_lock:
            pomo_active = pomo is not None
            if pomo_active and time.time() >= pomo["until"]:
                pomo_next(counted=True)
        if pomo_active:
            time.sleep(1)
            continue
        if dsa_up and mode in ("idle", "coding"):
            dsa_auto = True
            set_mode("dsa")
        elif mode == "dsa" and dsa_auto and not dsa_up:
            dsa_auto = False
            set_mode("coding" if nvim_up else "idle")
        elif mode == "idle" and nvim_up:
            set_mode("coding")
        elif mode == "coding" and not nvim_up:
            set_mode("idle")
        # Music mode is owned by music_loop. Do not time it out based on lyric
        # changes: instrumental tracks, plain lyrics, and long lyric gaps are valid.
        time.sleep(1)



def music_loop():
    """Now-playing + synced lyrics -> the `music` layer.

    Music is NOT a mode: it never touches set_mode(), so dsa / pomodoro / coding / idle carry on
    exactly as before while the lyrics (and the vibing) follow the player on top."""
    global last_track
    if subprocess.run(["sh", "-c", "command -v playerctl >/dev/null 2>&1"]).returncode != 0:
        print("playerctl not installed: music lyrics integration disabled")
        return
    np = NowPlaying(os.environ.get("BUDDY_PLAYER"))
    lyr = Lyrics()
    np.start()
    active_line, quiet_since = None, None
    while True:
        avail, artist, title, album, length, pos, playing, estimated = np.state()
        if avail and title and playing:
            quiet_since = None
            name = f"{clean_artist(artist)} — {title}" if artist else title
            lyr.update(artist, title, album, length)
            status, cur, win = lyr.window_ids(pos + lyric_offset, 1, 1)
            line = next((txt for idx, txt in win if idx == cur), "") if status == "synced" else ""
            if not music["on"] or music["name"] != name or music["status"] != status:
                music.update(on=True, name=name, status=status, line=line)
                active_line, last_track = line, (artist, title)
                send({"type": "music", **music})
            elif line != active_line:
                active_line = music["line"] = line
                send({"type": "lyric", "line": line, "artist": clean_artist(artist), "title": title,
                      "status": status, "playing": playing, "position": pos})
        else:
            quiet_since = quiet_since or time.time()
            if music["on"] and time.time() - quiet_since > 3:      # ride out the gap between tracks
                music.update(on=False, name="", line="", status="")
                active_line = last_track = None
                send({"type": "music", **music})
        time.sleep(0.2)

# ---- automatic reactions to what the PC is doing ----
cooldowns = {}


def ready(name, secs):
    """True (and arms the cooldown) if `name` hasn't fired in the last `secs` seconds."""
    now = time.time()
    if now - cooldowns.get(name, 0) < secs:
        return False
    cooldowns[name] = now
    return True


def react(mood, text="", secs=3.0):
    send({"type": "flash", "mood": mood, "text": text, "ms": int(secs * 1000)})


# `cmd` events come from hooks/buddy-shell.sh: {"type":"cmd","line":...,"code":N,"secs":N}.
# The line is only classified here (git / build / test / error); it is never stored or re-broadcast.
SKIP_ERR = {"grep", "egrep", "fgrep", "rg", "diff", "cmp", "test", "[", "[[", "which", "pgrep",
            "less", "fzf", "cd", "type", "command"}              # non-zero is normal for these
IGNORE_CODES = {130, 131, 141, 143, 146, 148}                    # Ctrl-C/Z, SIGPIPE, SIGTERM
WRAPPERS = {"sudo", "time", "command", "env", "nice", "exec", "nohup"}
BUILD_BIN = {"make", "cmake", "ninja", "gcc", "g++", "cc", "c++", "clang", "clang++", "rustc",
             "javac", "tsc", "zig", "ctest", "pytest", "tox", "mvn", "gradle", "meson"}
BUILD_SUB = {"cargo": {"build", "test", "check", "clippy", "bench"}, "go": {"build", "test", "vet"},
             "npm": {"test", "build"}, "yarn": {"test", "build"}, "pnpm": {"test", "build"}}


def last_command(line):
    """Tokens of the command whose exit status the shell reported: the last of `a && b ; c`, first of a pipe."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        toks = line.split()
    seg = []
    for tk in toks:
        if tk in ("&&", "||", ";"):
            seg = []
        else:
            seg.append(tk)
    if "|" in seg:
        seg = seg[:seg.index("|")]
    while seg and (seg[0] in WRAPPERS or re.match(r"^[A-Za-z_]\w*=", seg[0])):
        seg.pop(0)
    return seg


def on_cmd(ev):
    """A shell command finished: git commit/push, build/test result, or a plain error."""
    global fail_streak
    try:
        code, secs = int(ev.get("code", 0)), float(ev.get("secs", 0))
    except (TypeError, ValueError):
        return
    toks = last_command(str(ev.get("line", "")))
    if not toks or code in IGNORE_CODES:
        return
    prog, rest = os.path.basename(toks[0]), toks[1:]
    if prog == "git":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-C", "-c") else 1
        sub_cmd, args = (rest[i], rest[i + 1:]) if i < len(rest) else ("", [])
        if sub_cmd == "commit" and code == 0 and "--dry-run" not in args:
            return react("happy", "nice", 2)
        if sub_cmd == "push":
            force = any(a.startswith("--force") or a.startswith("+") or
                        (a.startswith("-") and not a.startswith("--") and "f" in a) for a in args)
            if force:                                         # the shell sees the literal flag; git hooks can't
                return react("panic", "force push?!", 3.5)
            return react("happy", "pushed", 2) if code == 0 else react("flinch", "rejected", 2.5)
        if sub_cmd == "reset" and "--hard" in args:
            return react("flinch", "...gone.", 2.5)
        if sub_cmd in ("merge", "rebase", "cherry-pick") and code != 0:
            return react("worry", "conflict?", 3)
    is_build = prog in BUILD_BIN
    if not is_build and prog in ("python", "python3"):
        is_build = rest[:2] in (["-m", "pytest"], ["-m", "unittest"])
    if not is_build and prog in BUILD_SUB:
        args = rest[1:] if rest[:1] == ["run"] else rest
        is_build = bool(args) and args[0] in BUILD_SUB[prog]
    if is_build:
        if code == 0:
            prev, fail_streak = fail_streak, 0
            react("overjoyed" if prev >= 3 else "happy", "finally." if prev >= 3 else "passed", 2.5)
        else:
            fail_streak += 1
            react("worry" if fail_streak >= 3 else "flinch", "again?" if fail_streak >= 3 else "failed", 2.5)
    elif code == 127:
        react("flinch", "typo?", 2.5)
    elif code != 0:
        if prog not in SKIP_ERR and ready("term_err", 3):
            react("flinch", f"exit {code}", 2.2)
    elif secs >= 30:
        react("happy", "done", 2.5)


_prev_cpu = None


def cpu_percent():
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            p = list(map(int, f.readline().split()[1:]))
    except (OSError, ValueError):
        return 0
    idle, total = p[3] + p[4], sum(p)
    prev, _prev_cpu = _prev_cpu, (idle, total)
    if not prev or total == prev[1]:
        return 0
    return int(100 * (1 - (idle - prev[0]) / (total - prev[1])))


def read_battery():
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        try:
            with open(d + "/capacity") as a, open(d + "/status") as b:
                return int(a.read()), b.read().strip()
        except (OSError, ValueError):
            pass
    return None, None


def net_up():
    try:
        with open("/proc/net/route") as f:
            return any(len(l.split()) > 1 and l.split()[1] == "00000000" for l in list(f)[1:])
    except OSError:
        return True


def watch_loop():
    hi = runaway = 0
    prev_status, bat_warned = None, False
    net_state, net_votes = True, 0
    sit_since = None
    n = 0
    while True:
        time.sleep(1)
        n += 1
        now = time.time()
        # CPU: sustained load -> strain, a runaway process -> panic
        cpu = cpu_percent()
        hi = hi + 1 if cpu >= 90 else 0
        runaway = runaway + 1 if cpu >= 97 else 0
        if runaway >= 25 and ready("runaway", 120):
            react("panic", "something is eating the cpu", 5)
        elif hi >= 6 and ready("cpu", 60):
            react("strain", f"cpu {cpu}%", 6)
        # battery: low warning, plug-in cheer
        cap, status = read_battery()
        if cap is not None:
            if status != "Discharging" or cap > 25:
                bat_warned = False
            if status == "Discharging" and cap <= 10 and ready("bat10", 120):
                react("panic", f"battery {cap}%!", 5)
            elif status == "Discharging" and cap <= 20 and not bat_warned:
                bat_warned = True
                react("worry", f"battery {cap}%", 5)
            if prev_status == "Discharging" and status in ("Charging", "Full", "Not charging"):
                react("happy", "plugged in", 2.5)
            prev_status = status
        # network: needs 3 identical readings in a row so a blip doesn't count
        up = net_up()
        net_votes = net_votes + 1 if up != net_state else 0
        if net_votes >= 3:
            net_state, net_votes = up, 0
            react("happy" if up else "worry", "back online" if up else "offline", 3)
        # typing speed (presses per second over the last 3s)
        kps = sum(1 for t in keys if now - t < 3) / 3.0
        send({"type": "typing", "kps": round(kps, 1)})
        if n % 2 == 0:
            send({"type": "sys", "cpu": cpu, "battery": cap, "online": net_state})
        # late night + sitting too long (only while you're actually at the machine)
        active = now - last_move < 60
        if 1 <= time.localtime().tm_hour < 5 and active and ready("late", 2700):
            react("judge", "go to bed", 6)
        if now - last_move < 120:
            sit_since = sit_since or now
        elif now - last_move > 300:
            sit_since = None
        if sit_since and now - sit_since > SIT_MIN * 60 and ready("sit", 2400):
            react("yawn", "stand up for a minute", 6)


def notify_loop():
    """Desktop notification arrived -> a glance toward the corner. Only the fact of it is used, never the content."""
    if not shutil.which("dbus-monitor"):
        print("dbus-monitor not found: no notification glances")
        return
    p = subprocess.Popen(["dbus-monitor", "--session", "interface='org.freedesktop.Notifications',member='Notify'"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    last = 0.0
    for line in p.stdout:
        if "member=Notify" in line and time.time() - last > 1:
            last = time.time()
            send({"type": "notify"})


# ---- http ----
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue()
            q.put({"type": "state", "state": snap()})
            q.put({"type": "music", **music})
            try:
                with open(dsa_history_path) as hf: q.put({"type": "dsa_history", "history": json.load(hf)})
            except (OSError, ValueError): pass
            with lock:
                clients.add(q)
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    clients.discard(q)
            return
        body = None
        for d in ("public", ""):  # works whether or not index.html sits in public/
            try:
                with open(os.path.join(HERE, d, "index.html"), "rb") as f:
                    body = f.read()
                break
            except OSError:
                pass
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/event":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            handle(json.loads(self.rfile.read(n)))
            self.send_response(204)
        except (ValueError, json.JSONDecodeError):
            self.send_response(400)
        self.end_headers()


if __name__ == "__main__":
    wayland = SESSION == "wayland"
    if not wayland:
        threading.Thread(target=xdotool_loop, daemon=True).start()
    threading.Thread(target=input_loop, args=(wayland,), daemon=True).start()
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=music_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    threading.Thread(target=notify_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print(f"buddy on :{PORT}")
    srv.serve_forever()

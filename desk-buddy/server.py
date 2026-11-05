#!/usr/bin/env python3
"""desk-buddy server (stdlib only). Hooks POST /event, the phone listens on /events (SSE)."""
import glob, json, os, queue, re, select, struct, subprocess, threading, time
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
pomo = None                           # running pomodoro: n, focus/short/long minutes, phase, until
pomo_lock = threading.Lock()


def send(ev):
    with lock:
        for q in clients:
            q.put(ev)


def snap():
    """Copy of state. A timed mode (pomo) also gets ms left: the phone's clock may differ, so send a duration."""
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
    if t == "mode":
        with pomo_lock:
            pomo = None               # picking a mode by hand ends a running pomodoro
        return set_mode(ev.get("value") or "idle", ev.get("text") or "")
    if t == "lyric":
        last_lyric = time.time()
        if state["mode"] in ("idle", "coding"):
            set_mode("music")
    send(ev)  # flash, lyric, anything else: pass straight to the phone


# ---- pomodoro ----
# POST /event {"type":"pomo","action":"start"|"skip"|"stop"}; start also takes focus/short/long (minutes).
# The timer lives here, not on the phone, so reloading the page never loses it.
# Every 4th focus session earns the long break. "pomo" is sticky: lyrics/nvim/dsa don't override it.
def pomo_phase(phase, minutes):
    pomo["phase"] = phase
    pomo["until"] = time.time() + minutes * 60
    done = 4 if phase == "long" else pomo["n"] % 4
    set_mode("pomo", phase, phase=phase, until=int(pomo["until"] * 1000), done=done)


def pomo_next(counted):
    """Advance one phase. Caller holds pomo_lock. A skipped focus session isn't counted."""
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
            mins = lambda k, d: max(0.1, float(ev.get(k, d)))
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
        with pomo_lock:
            if pomo and time.time() >= pomo["until"]:
                pomo_next(counted=True)
        r = run(["xprintidle"])
        ms = int(r.stdout) if r and r.returncode == 0 else int((time.time() - last_move) * 1000)
        send({"type": "idle", "ms": ms})
        r = run(["pgrep", "-x", "nvim"])
        nvim_up = bool(r and r.returncode == 0)
        dsa_up = dsa_session_up()
        mode = state["mode"]
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
        elif mode == "music" and time.time() - last_lyric > 20:
            set_mode("coding" if nvim_up else "idle")
        time.sleep(1)


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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print(f"buddy on :{PORT}")
    srv.serve_forever()

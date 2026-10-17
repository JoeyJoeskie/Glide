#!/usr/bin/env python3
"""
Glide - one mouse & keyboard, every screen.

A free, open-source software KVM. Run the receiver on your second PC, run the
controller on the PC whose mouse/keyboard you want to share. Then just glide
your cursor off the screen edge (default: right) and it crosses to the other
PC, like one big desktop spanning both machines. Glide back the other way to
return. Ctrl+Alt+G is a manual toggle / failsafe. Edge detection and crossing
use the whole virtual desktop, so it works with multiple monitors.

Original clean-room implementation. MIT licensed. Windows.

Usage:
    glide.py receiver                       # the PC being controlled
    glide.py controller --host 192.168.1.42 # the PC you sit at
    glide.py controller                     # will prompt for the receiver IP
    glide.py --selftest                     # run the built-in checks
"""
import argparse
import ctypes
import json
import queue
import socket
import struct
import sys
import threading
import time

__version__ = "0.9.3"
DEFAULT_PORT = 24800

# ---------------------------------------------------------------------------
# Wire protocol: newline-delimited JSON, one event per line.
#   receiver -> controller on connect:
#     {"t":"hello","x0":int,"y0":int,"w":int,"h":int}   its virtual-desktop box
#   controller -> receiver:
#     {"t":"mva","x":int,"y":int}            absolute cursor position (physical px)
#     {"t":"btn","b":"left|right|middle","p":bool}
#     {"t":"scroll","dx":int,"dy":int}
#     {"t":"key","vk":int,"p":bool}          virtual-key code, press/release
# ---------------------------------------------------------------------------


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def local_ip() -> str:
    """Best-guess LAN IP of this machine (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def iter_messages(sock: socket.socket, buf=b""):
    """Yield decoded messages from a socket, framed by newlines."""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                yield json.loads(line)


def enable_dpi_awareness():
    # Make screen metrics, SetCursorPos and the low-level hook all agree on
    # PHYSICAL pixels. Without this, on a scaled/high-DPI display the hook and
    # metrics use different coordinate scales and crossing geometry is wrong.
    for attempt in (
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ):
        try:
            if attempt():
                return
        except Exception:
            continue


def virtual_box(user32):
    """(x0, y0, w, h) of the whole virtual desktop in physical pixels."""
    x0 = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    y0 = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)    # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)    # SM_CYVIRTUALSCREEN
    return x0, y0, w, h


# --- clipboard (Windows CF_UNICODETEXT) -----------------------------------
if sys.platform == "win32":
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002
    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32
    _u32.GetClipboardData.restype = ctypes.c_void_p
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalLock.argtypes = [ctypes.c_void_p]
    _k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _k32.GlobalAlloc.restype = ctypes.c_void_p
    _k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]


def get_clipboard_text():
    if sys.platform != "win32" or not _u32.OpenClipboard(0):
        return None
    try:
        h = _u32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        p = _k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            _k32.GlobalUnlock(h)
    except Exception:
        return None
    finally:
        _u32.CloseClipboard()


def set_clipboard_text(s):
    if sys.platform != "win32" or not _u32.OpenClipboard(0):
        return
    try:
        _u32.EmptyClipboard()
        data = s.encode("utf-16-le") + b"\x00\x00"
        h = _k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        p = _k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        _k32.GlobalUnlock(h)
        _u32.SetClipboardData(_CF_UNICODETEXT, h)   # ownership passes to clipboard
    except Exception:
        pass
    finally:
        _u32.CloseClipboard()


# ---------------------------------------------------------------------------
# Receiver: announce its screen box, then inject whatever the controller sends.
# ---------------------------------------------------------------------------

def run_receiver(port: int, code: str = ""):
    from pynput import mouse, keyboard

    enable_dpi_awareness()
    user32 = ctypes.windll.user32
    x0, y0, w, h = virtual_box(user32)

    mc = mouse.Controller()
    kc = keyboard.Controller()
    buttons = {"left": mouse.Button.left,
               "right": mouse.Button.right,
               "middle": mouse.Button.middle}

    def handle(msg: dict):
        t = msg.get("t")
        if t == "mva":
            user32.SetCursorPos(int(msg["x"]), int(msg["y"]))
        elif t == "mv":                      # legacy relative move
            mc.move(msg["dx"], msg["dy"])
        elif t == "btn":
            b = buttons.get(msg["b"])
            if b:
                (mc.press if msg["p"] else mc.release)(b)
        elif t == "scroll":
            mc.scroll(msg["dx"], msg["dy"])
        elif t == "key":
            k = keyboard.KeyCode.from_vk(msg["vk"])
            try:
                (kc.press if msg["p"] else kc.release)(k)
            except Exception as e:
                print(f"  key {msg['vk']} failed: {e}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(1)
    ip = local_ip()
    print(f"Glide receiver {__version__} listening on port {port}.")
    print(f"On the controller PC, use this IP:  {ip}")
    print(f"   controller command:  glide.exe controller --host {ip}")
    print("Waiting for a controller to connect... (Ctrl+C to quit)")
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # low-latency inject
        gen = iter_messages(conn)
        try:
            first = next(gen, None)          # controller must authenticate first
            if not first or first.get("t") != "auth" or str(first.get("code", "")) != str(code):
                conn.sendall(encode({"t": "denied"}))
                print(f"Rejected {addr[0]}: wrong or missing password.")
                conn.close()
                continue
            conn.sendall(encode({"t": "hello", "x0": x0, "y0": y0, "w": w, "h": h}))
            print(f"Controller connected from {addr[0]}. Control is live.")
            last_clip = {"v": get_clipboard_text()}
            stop = threading.Event()

            def clip_watch():
                while not stop.is_set():
                    t = get_clipboard_text()
                    if t and t != last_clip["v"]:
                        last_clip["v"] = t
                        try:
                            conn.sendall(encode({"t": "clip", "text": t}))
                        except OSError:
                            return
                    stop.wait(0.4)

            threading.Thread(target=clip_watch, daemon=True).start()
            try:
                for msg in gen:
                    if msg.get("t") == "clip":
                        last_clip["v"] = msg["text"]
                        set_clipboard_text(msg["text"])
                    else:
                        handle(msg)
            finally:
                stop.set()
        except (ConnectionResetError, OSError, json.JSONDecodeError) as e:
            print(f"Connection dropped: {e}")
        finally:
            conn.close()
            print("Controller disconnected. Waiting for reconnect...")


# ---------------------------------------------------------------------------
# Controller: capture local input, model the remote cursor, forward absolute.
# ---------------------------------------------------------------------------

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_G = 0x47
CTRL_VKS = {0x11, 0xA2, 0xA3}
ALT_VKS = {0x12, 0xA4, 0xA5}
MOD_VKS = CTRL_VKS | ALT_VKS

BTN_DOWN = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle"}
BTN_UP = {WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle"}


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Controller:
    def __init__(self, host, port, edge="right", code=""):
        self.host = host
        self.port = port
        self.edge = edge
        self.code = code
        self.sock = None
        self.active = False
        enable_dpi_awareness()
        self.user32 = ctypes.windll.user32
        self.vL, self.vT, self.vW, self.vH = virtual_box(self.user32)
        self.vR, self.vB = self.vL + self.vW - 1, self.vT + self.vH - 1
        self.cx, self.cy = self.vL + self.vW // 2, self.vT + self.vH // 2  # warp target
        # remote screen box (filled from the receiver's hello); own box as fallback
        self.rx0, self.ry0, self.rw, self.rh = self.vL, self.vT, self.vW, self.vH
        self.rx = self.ry = 0            # modelled remote cursor (0..rw-1, 0..rh-1)
        self._mods = set()
        self._ml = self._kl = None
        self._q = queue.Queue()
        self._rx = None              # inbound message generator (set in connect)
        self._last_clip = None       # last clipboard text seen/applied (echo guard)

    # -- networking --------------------------------------------------------
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._rx = iter_messages(self.sock)
        self.sock.sendall(encode({"t": "auth", "code": self.code}))   # authenticate first
        self._handshake()
        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._clip_watch, daemon=True).start()

    def _handshake(self):
        # Expect the receiver's screen box, or a refusal if the password is wrong.
        self.sock.settimeout(5)
        try:
            msg = next(self._rx, None)
        except (OSError, json.JSONDecodeError):
            msg = None
        finally:
            self.sock.settimeout(None)
        if msg and msg.get("t") == "denied":
            raise PermissionError("receiver refused the password")
        if msg and msg.get("t") == "hello":
            self.rx0, self.ry0 = msg["x0"], msg["y0"]
            self.rw, self.rh = msg["w"], msg["h"]
        else:
            print("No hello from receiver (old version?); assuming same screen size.")

    def _reader_loop(self):
        # Inbound from the receiver: currently just clipboard updates.
        try:
            for msg in self._rx:
                if msg.get("t") == "clip":
                    self._last_clip = msg["text"]
                    set_clipboard_text(msg["text"])
        except (OSError, json.JSONDecodeError):
            pass

    def _clip_watch(self):
        # Push our clipboard to the receiver whenever it changes.
        while True:
            t = get_clipboard_text()
            if t and t != self._last_clip:
                self._last_clip = t
                self.send({"t": "clip", "text": t})
            time.sleep(0.4)

    def send(self, msg: dict):
        self._q.put(msg)             # enqueue only; sender thread does the write

    def _sender_loop(self):
        while True:
            msg = self._q.get()
            if msg is None:
                return
            batch = [msg]
            try:
                while True:
                    batch.append(self._q.get_nowait())
            except queue.Empty:
                pass
            stop = None in batch
            for m in self._coalesce([x for x in batch if x is not None]):
                try:
                    self.sock.sendall(encode(m))
                except OSError as e:
                    print(f"Send failed ({e}); control back on THIS PC.")
                    self.release()
                    return
            if stop:
                return

    @staticmethod
    def _coalesce(msgs):
        # Collapse a backlog: consecutive absolute moves -> keep the latest;
        # consecutive legacy relative moves -> sum. Clicks/keys break the run.
        out = []
        for m in msgs:
            t = m.get("t")
            if t == "mva" and out and out[-1]["t"] == "mva":
                out[-1] = m
            elif t == "mv" and out and out[-1]["t"] == "mv":
                out[-1] = {"t": "mv", "dx": out[-1]["dx"] + m["dx"],
                           "dy": out[-1]["dy"] + m["dy"]}
            else:
                out.append(m)
        return out

    # -- cursor helpers ----------------------------------------------------
    def warp_center(self):
        self.user32.SetCursorPos(self.cx, self.cy)

    def _push_remote(self):
        self.send({"t": "mva", "x": self.rx0 + self.rx, "y": self.ry0 + self.ry})

    # -- geometry ----------------------------------------------------------
    def _at_edge(self, x, y):
        e = self.edge
        return ((e == "right" and x >= self.vR) or (e == "left" and x <= self.vL) or
                (e == "top" and y <= self.vT) or (e == "bottom" and y >= self.vB))

    def _entry_from(self, x, y):
        # Where the remote cursor appears when we cross in: opposite edge, with
        # the perpendicular position mapped proportionally from this screen.
        fx = _clamp((x - self.vL) / max(1, self.vW), 0.0, 1.0)
        fy = _clamp((y - self.vT) / max(1, self.vH), 0.0, 1.0)
        if self.edge == "right":
            return 0, int(fy * (self.rh - 1))
        if self.edge == "left":
            return self.rw - 1, int(fy * (self.rh - 1))
        if self.edge == "top":
            return int(fx * (self.rw - 1)), 0
        return int(fx * (self.rw - 1)), self.rh - 1          # bottom

    def _past_far_edge(self, nrx, nry):
        # Dragged back out the entry edge -> hand control back to this PC.
        e = self.edge
        return ((e == "right" and nrx < 0) or (e == "left" and nrx > self.rw - 1) or
                (e == "top" and nry < 0) or (e == "bottom" and nry > self.rh - 1))

    # -- control handoff ---------------------------------------------------
    def enter(self, rx, ry):
        self.active = True
        self.rx, self.ry = _clamp(rx, 0, self.rw - 1), _clamp(ry, 0, self.rh - 1)
        self.warp_center()
        self._push_remote()
        print(">>> NOW CONTROLLING THE OTHER PC   (glide back / Ctrl+Alt+G to return)")

    def release(self, park=None):
        if not self.active:
            return
        self.active = False
        for vk in list(self._mods):          # don't leave Ctrl/Alt stuck on the remote
            self.send({"t": "key", "vk": vk, "p": False})
        if park:
            self.user32.SetCursorPos(*park)
        print("<<< BACK ON THIS PC                (Ctrl+Alt+G / glide over to switch)")

    def toggle(self):
        if self.active:
            self.release(park=(self.cx, self.cy))
        else:
            self.enter(self.rw // 2, self.rh // 2)   # hotkey entry: remote center

    def _park_inside_edge(self):
        # Return to this screen at the SAME relative spot the remote cursor had,
        # mapped across the two screens' sizes (so it doesn't snap to center).
        e = self.edge
        py = int(self.vT + (self.ry / max(1, self.rh - 1)) * self.vH)
        px = int(self.vL + (self.rx / max(1, self.rw - 1)) * self.vW)
        py = _clamp(py, self.vT + 2, self.vB - 2)
        px = _clamp(px, self.vL + 2, self.vR - 2)
        if e == "right":
            return (self.vR - 2, py)
        if e == "left":
            return (self.vL + 2, py)
        if e == "top":
            return (px, self.vT + 2)
        return (px, self.vB - 2)

    # -- event filters (return False => suppress locally; raised by run()) --
    def mouse_filter(self):
        def f(msg, data):
            if not self.active:
                if msg == WM_MOUSEMOVE and self._at_edge(data.pt.x, data.pt.y):
                    self.enter(*self._entry_from(data.pt.x, data.pt.y))
                    return False
                return True
            if msg == WM_MOUSEMOVE:
                dx, dy = data.pt.x - self.cx, data.pt.y - self.cy
                if abs(dx) > 500 or abs(dy) > 500:   # warp desync -> re-center, drop
                    self.warp_center()
                    return False
                if dx or dy:
                    nrx, nry = self.rx + dx, self.ry + dy
                    if self._past_far_edge(nrx, nry):
                        self.release(park=self._park_inside_edge())
                        return False
                    self.rx, self.ry = _clamp(nrx, 0, self.rw - 1), _clamp(nry, 0, self.rh - 1)
                    self._push_remote()
                    self.warp_center()
                return False
            if msg in BTN_DOWN:
                self.send({"t": "btn", "b": BTN_DOWN[msg], "p": True})
            elif msg in BTN_UP:
                self.send({"t": "btn", "b": BTN_UP[msg], "p": False})
            elif msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                raw = (data.mouseData >> 16) & 0xFFFF
                delta = struct.unpack("h", struct.pack("H", raw))[0] // 120
                self.send({"t": "scroll", "dx": 0, "dy": delta} if msg == WM_MOUSEWHEEL
                          else {"t": "scroll", "dx": delta, "dy": 0})
            return False
        return f

    def key_filter(self):
        def f(msg, data):
            vk = data.vkCode
            down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
            up = msg in (WM_KEYUP, WM_SYSKEYUP)
            if vk in MOD_VKS:
                self._mods.add(vk) if down else self._mods.discard(vk)
            if down and vk == VK_G and (self._mods & CTRL_VKS) and (self._mods & ALT_VKS):
                self.toggle()
                return False
            if not self.active:
                return True
            if down:
                self.send({"t": "key", "vk": vk, "p": True})
            elif up:
                self.send({"t": "key", "vk": vk, "p": False})
            return False
        return f

    def run(self):
        from pynput import mouse, keyboard
        self.connect()
        print(f"Glide controller {__version__} connected to {self.host}:{self.port}.")
        print(f"   this screen {self.vW}x{self.vH}   |   other screen {self.rw}x{self.rh}")
        print()
        print(f"   Glide the cursor off the {self.edge.upper()} edge  ->  drive the OTHER PC")
        print("   Glide back the other way (or Ctrl+Alt+G)  ->  return to THIS PC")
        print("   Close this window to quit.")
        print()
        minner, kinner = self.mouse_filter(), self.key_filter()

        def mouse_wf(msg, data):
            if minner(msg, data) is False:
                self._ml.suppress_event()

        def key_wf(msg, data):
            if kinner(msg, data) is False:
                self._kl.suppress_event()

        self._ml = mouse.Listener(win32_event_filter=mouse_wf, suppress=False)
        self._kl = keyboard.Listener(win32_event_filter=key_wf, suppress=False)
        self._ml.start()
        self._kl.start()
        try:
            while self._ml.running and self._kl.running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self._q.put(None)
            self._ml.stop()
            self._kl.stop()


# ---------------------------------------------------------------------------
# Built-in checks (no network peer, no real mouse needed).
# ---------------------------------------------------------------------------

def wire_test():
    got = []
    a, b = socket.socketpair()
    reader = threading.Thread(target=lambda: got.extend(iter_messages(a)), daemon=True)
    reader.start()
    sent = [
        {"t": "mva", "x": 5, "y": 7},
        {"t": "btn", "b": "left", "p": True},
        {"t": "scroll", "dx": 0, "dy": 2},
        {"t": "key", "vk": 65, "p": False},
    ]
    for m in sent:
        b.sendall(encode(m))
    time.sleep(0.2)
    b.close()
    a.close()
    assert got == sent, f"roundtrip mismatch:\n sent {sent}\n got  {got}"
    print("wire test OK: 4/4 messages survived the round trip.")


def controller_logic_test():
    """Feed fake Win32 events through the filters; never touches a real mouse."""
    class Pt:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class MData:
        def __init__(self, x, y, mouse_data=0):
            self.pt = Pt(x, y)
            self.mouseData = mouse_data

    class KData:
        def __init__(self, vk):
            self.vkCode = vk

    c = Controller("127.0.0.1", DEFAULT_PORT, "right")
    c.warp_center = lambda: None          # do NOT move the real cursor
    c.rx0, c.ry0, c.rw, c.rh = 0, 0, 1000, 800   # pretend remote is 1000x800
    c.sent = []
    c.send = c.sent.append
    mf, kf = c.mouse_filter(), c.key_filter()

    # idle: a move in the middle passes through locally, nothing sent
    assert mf(WM_MOUSEMOVE, MData(c.cx, c.cy)) is True
    assert c.sent == []

    # glide to the far RIGHT edge -> cross in at the remote's LEFT edge (rx 0)
    assert mf(WM_MOUSEMOVE, MData(c.vR, c.cy)) is False
    assert c.active is True and c.rx == 0
    assert c.sent[-1] == {"t": "mva", "x": 0, "y": c.ry0 + c.ry}

    # roam RIGHT into the remote: absolute x grows, stays active
    assert mf(WM_MOUSEMOVE, MData(c.cx + 40, c.cy)) is False
    assert c.active is True and c.rx == 40
    assert c.sent[-1] == {"t": "mva", "x": 40, "y": c.ry0 + c.ry}

    # roam back LEFT but still on-screen: stays active (the old "invisible wall" bug)
    assert mf(WM_MOUSEMOVE, MData(c.cx - 30, c.cy)) is False
    assert c.active is True and c.rx == 10

    # a left click and a letter key forward while active
    assert mf(WM_LBUTTONDOWN, MData(c.cx, c.cy)) is False
    assert c.sent[-1] == {"t": "btn", "b": "left", "p": True}
    assert kf(WM_KEYDOWN, KData(65)) is False
    assert c.sent[-1] == {"t": "key", "vk": 65, "p": True}

    # push past the remote's LEFT edge -> hand control back to THIS PC
    assert mf(WM_MOUSEMOVE, MData(c.cx - 20, c.cy)) is False
    assert c.active is False

    # absolute moves coalesce to the latest; clicks keep order
    merged = Controller._coalesce([
        {"t": "mva", "x": 1, "y": 1}, {"t": "mva", "x": 2, "y": 2},
        {"t": "btn", "b": "left", "p": True}, {"t": "mva", "x": 9, "y": 9}])
    assert merged == [{"t": "mva", "x": 2, "y": 2},
                      {"t": "btn", "b": "left", "p": True},
                      {"t": "mva", "x": 9, "y": 9}]

    print("controller logic OK: cross-in, roam both ways, far-edge return, coalesce.")


def selftest():
    wire_test()
    if sys.platform == "win32":
        controller_logic_test()
    else:
        print("(controller logic test skipped: not on Windows)")
    print("ALL CHECKS PASSED.")


def main():
    p = argparse.ArgumentParser(description="Glide - one mouse & keyboard, every screen.")
    p.add_argument("role", nargs="?", choices=["receiver", "controller"])
    p.add_argument("--host", help="receiver IP (controller mode)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--edge", default="right", choices=["left", "right", "top", "bottom"],
                   help="screen edge that glides to the other PC (default: right)")
    p.add_argument("--code", default="", help="connection password (shared on both PCs)")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return

    if args.role is None:
        print(__doc__)
        role = input("Run as [receiver/controller]? ").strip().lower()
        args.role = "controller" if role.startswith("c") else "receiver"

    if sys.platform != "win32" and args.role == "controller":
        print("Controller mode uses Windows low-level hooks; run it on Windows.")
        sys.exit(1)

    if args.role == "receiver":
        code = args.code or input("Set a connection password (the other PC must enter it): ").strip()
        try:
            run_receiver(args.port, code)
        except KeyboardInterrupt:
            print("\nBye.")
    else:
        host = args.host or input("Receiver IP address: ").strip()
        if not host:
            print("No receiver IP given.")
            sys.exit(1)
        code = args.code or input("Receiver's password: ").strip()
        try:
            Controller(host, args.port, args.edge, code).run()
        except PermissionError:
            print("Receiver refused the connection: wrong password.")
            sys.exit(1)
        except (ConnectionRefusedError, OSError) as e:
            print(f"Could not reach receiver {host}:{args.port} -> {e}")
            print("Is the receiver running and the port open in the firewall?")
            sys.exit(1)


if __name__ == "__main__":
    main()

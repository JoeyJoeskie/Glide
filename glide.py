#!/usr/bin/env python3
"""
Glide - one mouse & keyboard, every screen.

A free, open-source software KVM. Run the receiver on your second PC, run the
controller on the PC whose mouse/keyboard you want to share. Then just press
CTRL+ALT+G to send your mouse & keyboard to the other PC, and Ctrl+Alt+G
again to come back. Simple and reliable - no edge-sliding to misfire.

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
import socket
import struct
import sys
import threading
import time

__version__ = "0.2.0"
DEFAULT_PORT = 24800

# ---------------------------------------------------------------------------
# Wire protocol: newline-delimited JSON, one event per line.
#   {"t":"mv","dx":int,"dy":int}            relative mouse motion
#   {"t":"btn","b":"left|right|middle","p":bool}
#   {"t":"scroll","dx":int,"dy":int}
#   {"t":"key","vk":int,"p":bool}           virtual-key code, press/release
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


def iter_messages(sock: socket.socket):
    """Yield decoded messages from a socket, framed by newlines."""
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                yield json.loads(line)


# ---------------------------------------------------------------------------
# Receiver: listen, inject whatever the controller sends.
# ---------------------------------------------------------------------------

def run_receiver(port: int):
    from pynput import mouse, keyboard

    mc = mouse.Controller()
    kc = keyboard.Controller()
    buttons = {"left": mouse.Button.left,
               "right": mouse.Button.right,
               "middle": mouse.Button.middle}

    def handle(msg: dict):
        t = msg.get("t")
        if t == "mv":
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
        print(f"Controller connected from {addr[0]}. Control is live.")
        try:
            for msg in iter_messages(conn):
                handle(msg)
        except (ConnectionResetError, OSError, json.JSONDecodeError) as e:
            print(f"Connection dropped: {e}")
        finally:
            conn.close()
            print("Controller disconnected. Waiting for reconnect...")


# ---------------------------------------------------------------------------
# Controller: capture local input, forward it while "active" (Scroll Lock on).
# ---------------------------------------------------------------------------

# Win32 message codes
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
# Switch hotkey: Ctrl + Alt + G. Track every Ctrl/Alt variant the hook reports.
VK_G = 0x47
CTRL_VKS = {0x11, 0xA2, 0xA3}
ALT_VKS = {0x12, 0xA4, 0xA5}
MOD_VKS = CTRL_VKS | ALT_VKS

BTN_DOWN = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle"}
BTN_UP = {WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle"}


class Controller:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.active = False          # True = input is being forwarded
        self.user32 = ctypes.windll.user32
        self.W = self.user32.GetSystemMetrics(0)
        self.H = self.user32.GetSystemMetrics(1)
        self.cx, self.cy = self.W // 2, self.H // 2
        self._mods = set()           # Ctrl/Alt keys currently held down

    # -- networking --------------------------------------------------------
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

    def send(self, msg: dict):
        try:
            self.sock.sendall(encode(msg))
        except OSError as e:
            print(f"Send failed ({e}); control back on THIS PC.")
            self.release()

    def warp_center(self):
        # Keep the real cursor pinned to screen center so every physical move
        # produces a fresh delta we can forward (works on any monitor layout).
        self.user32.SetCursorPos(self.cx, self.cy)

    # -- control handoff (Scroll Lock) -------------------------------------
    def grab(self):
        self.active = True
        self.warp_center()
        print(">>> NOW CONTROLLING THE OTHER PC   (Ctrl+Alt+G to come back)")

    def release(self):
        if self.active:
            self.active = False
            print("<<< BACK ON THIS PC                (Ctrl+Alt+G to switch again)")

    def toggle(self):
        self.release() if self.active else self.grab()

    # -- event filters -----------------------------------------------------
    # pynput suppresses the event when the filter returns False; True/None
    # lets it reach the local machine normally. Keep these fast: they run in
    # the low-level hook thread and Windows unhooks us if we stall.
    # ponytail: send() blocks on sendall here; add a sender queue if LAN lags.
    def mouse_filter(self):
        def f(msg, data):
            if not self.active:
                return True                       # pass everything through locally
            if msg == WM_MOUSEMOVE:
                dx, dy = data.pt.x - self.cx, data.pt.y - self.cy
                if dx or dy:                      # ignore our own re-center echo
                    self.send({"t": "mv", "dx": dx, "dy": dy})
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
            # Track Ctrl/Alt so we can spot the switch combo (works local or active).
            if vk in MOD_VKS:
                self._mods.add(vk) if down else self._mods.discard(vk)
            # Ctrl + Alt + G toggles control; swallow the G so it isn't typed.
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
        print()
        print("   Press CTRL+ALT+G  ->  your mouse & keyboard drive the OTHER PC")
        print("   Press CTRL+ALT+G  ->  come back to THIS PC")
        print("   Close this window to quit.")
        print()
        ml = mouse.Listener(win32_event_filter=self.mouse_filter(), suppress=False)
        kl = keyboard.Listener(win32_event_filter=self.key_filter(), suppress=False)
        ml.start()
        kl.start()
        try:
            while ml.running and kl.running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            ml.stop()
            kl.stop()


# ---------------------------------------------------------------------------
# Built-in checks (no network peer, no real mouse needed).
# ---------------------------------------------------------------------------

def wire_test():
    got = []
    a, b = socket.socketpair()
    reader = threading.Thread(target=lambda: got.extend(iter_messages(a)), daemon=True)
    reader.start()
    sent = [
        {"t": "mv", "dx": 5, "dy": -3},
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

    c = Controller("127.0.0.1", DEFAULT_PORT)
    c.warp_center = lambda: None          # do NOT move the real cursor
    c.sent = []
    c.send = c.sent.append
    mf, kf = c.mouse_filter(), c.key_filter()

    # inactive: a move passes through locally, nothing is sent
    assert mf(WM_MOUSEMOVE, MData(c.cx + 9, c.cy)) is True
    assert c.sent == []

    # Ctrl+Alt+G -> active (G is swallowed, not typed)
    kf(WM_KEYDOWN, KData(0x11))                      # Ctrl down
    kf(WM_KEYDOWN, KData(0x12))                      # Alt down
    assert kf(WM_KEYDOWN, KData(VK_G)) is False      # G completes the combo
    assert c.active is True

    # active: a move forwards the delta-from-center and is suppressed locally
    assert mf(WM_MOUSEMOVE, MData(c.cx + 5, c.cy - 3)) is False
    assert c.sent[-1] == {"t": "mv", "dx": 5, "dy": -3}

    # active: left click and a letter key forward
    assert mf(WM_LBUTTONDOWN, MData(c.cx, c.cy)) is False
    assert c.sent[-1] == {"t": "btn", "b": "left", "p": True}
    assert kf(WM_KEYDOWN, KData(65)) is False
    assert c.sent[-1] == {"t": "key", "vk": 65, "p": True}

    # Ctrl+Alt still held: tapping G again -> inactive, moves pass through
    assert kf(WM_KEYDOWN, KData(VK_G)) is False
    assert c.active is False
    assert mf(WM_MOUSEMOVE, MData(c.cx + 2, c.cy + 2)) is True

    print("controller logic OK: switch, move, click, key, switch-back all correct.")


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
        try:
            run_receiver(args.port)
        except KeyboardInterrupt:
            print("\nBye.")
    else:
        host = args.host or input("Receiver IP address: ").strip()
        if not host:
            print("No receiver IP given.")
            sys.exit(1)
        try:
            Controller(host, args.port).run()
        except (ConnectionRefusedError, OSError) as e:
            print(f"Could not reach receiver {host}:{args.port} -> {e}")
            print("Is the receiver running and the port open in the firewall?")
            sys.exit(1)


if __name__ == "__main__":
    main()

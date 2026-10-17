#!/usr/bin/env python3
"""
Glide - one mouse & keyboard, every screen.

A free, open-source software KVM. Run the receiver on your second PC, run the
controller on the PC whose mouse/keyboard you want to share, and slide your
cursor off the screen edge onto the other machine. Scroll Lock toggles /
force-releases control at any time.

Original clean-room implementation. MIT licensed. Windows.

Usage:
    glide.py receiver                       # the PC being controlled
    glide.py controller --host 192.168.1.42 # the PC you sit at
    glide.py controller                     # will prompt for the receiver IP
    glide.py --selftest                     # wire-protocol self check
"""
import argparse
import ctypes
import json
import socket
import struct
import sys
import threading
import time

__version__ = "0.1.0"
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
    print(f"Glide receiver {__version__} listening on port {port}.")
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
# Controller: capture local input, forward it while "remote" is active.
# ---------------------------------------------------------------------------

# Win32 message codes
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_SCROLL = 0x91

BTN_DOWN = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle"}
BTN_UP = {WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle"}


class Controller:
    def __init__(self, host, port, edge):
        self.host = host
        self.port = port
        self.edge = edge
        self.sock = None
        self.active = False          # True = input is being forwarded
        self.user32 = ctypes.windll.user32
        self.W = self.user32.GetSystemMetrics(0)
        self.H = self.user32.GetSystemMetrics(1)
        self.cx, self.cy = self.W // 2, self.H // 2
        self.virt = 0.0              # accumulated travel past the edge
        self.lock = threading.Lock()

    # -- networking --------------------------------------------------------
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        print(f"Connected to receiver {self.host}:{self.port}.")

    def send(self, msg: dict):
        try:
            self.sock.sendall(encode(msg))
        except OSError as e:
            print(f"Send failed ({e}); releasing control.")
            self.release()

    def warp_center(self):
        self.user32.SetCursorPos(self.cx, self.cy)

    # -- control handoff ---------------------------------------------------
    def grab(self):
        self.active = True
        self.virt = 0.0
        self.warp_center()
        print("--> control on RECEIVER (Scroll Lock to return)")

    def release(self):
        if self.active:
            self.active = False
            print("<-- control back on THIS PC")

    def toggle(self):
        self.release() if self.active else self.grab()

    # -- event filters -----------------------------------------------------
    # pynput suppresses the event when the filter returns False; True/None
    # lets it reach the local machine normally. Keep these fast: they run in
    # the low-level hook thread and Windows unhooks us if we stall.
    # ponytail: send() does a blocking sendall here; a sender queue if LAN lags.
    def mouse_filter(self):
        def f(msg, data):
            if msg == WM_MOUSEMOVE:
                x, y = data.pt.x, data.pt.y
                if not self.active:
                    if self._hit_edge(x, y):
                        self.grab()
                        return False
                    return True
                dx, dy = x - self.cx, y - self.cy
                if dx == 0 and dy == 0:          # our own re-center echo
                    return False
                self.virt += dx                   # track travel to allow return
                self.send({"t": "mv", "dx": dx, "dy": dy})
                self.warp_center()
                if self._return_edge():
                    self.release()
                return False

            if not self.active:
                return True

            if msg in BTN_DOWN:
                self.send({"t": "btn", "b": BTN_DOWN[msg], "p": True})
            elif msg in BTN_UP:
                self.send({"t": "btn", "b": BTN_UP[msg], "p": False})
            elif msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                raw = (data.mouseData >> 16) & 0xFFFF
                delta = struct.unpack("h", struct.pack("H", raw))[0] // 120
                if msg == WM_MOUSEWHEEL:
                    self.send({"t": "scroll", "dx": 0, "dy": delta})
                else:
                    self.send({"t": "scroll", "dx": delta, "dy": 0})
            return False
        return f

    def key_filter(self):
        def f(msg, data):
            vk = data.vkCode
            if vk == VK_SCROLL:                   # manual toggle / failsafe
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self.toggle()
                return False                      # eat both down and up
            if not self.active:
                return True
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self.send({"t": "key", "vk": vk, "p": True})
            elif msg in (WM_KEYUP, WM_SYSKEYUP):
                self.send({"t": "key", "vk": vk, "p": False})
            return False
        return f

    def _hit_edge(self, x, y):
        if self.edge == "right":
            return x >= self.W - 1
        if self.edge == "left":
            return x <= 0
        if self.edge == "top":
            return y <= 0
        if self.edge == "bottom":
            return y >= self.H - 1
        return False

    def _return_edge(self):
        # Pull the cursor back far enough the other way to hand control back.
        span = self.W if self.edge in ("left", "right") else self.H
        if self.edge in ("right", "bottom"):
            return self.virt <= -span // 2
        return self.virt >= span // 2

    def run(self):
        from pynput import mouse, keyboard
        self.connect()
        print(f"Glide controller {__version__}. Edge '{self.edge}' -> receiver.")
        print("Slide the cursor to that edge to cross over. Scroll Lock = manual toggle.")
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
# Self-test: the wire protocol must survive a round trip (no hooks needed).
# ---------------------------------------------------------------------------

def selftest():
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
    print("selftest OK: 4/4 messages survived the wire round trip.")


def main():
    p = argparse.ArgumentParser(description="Glide - one mouse & keyboard, every screen.")
    p.add_argument("role", nargs="?", choices=["receiver", "controller"])
    p.add_argument("--host", help="receiver IP (controller mode)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--edge", default="right", choices=["left", "right", "top", "bottom"],
                   help="which screen edge crosses to the receiver")
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
            Controller(host, args.port, args.edge).run()
        except (ConnectionRefusedError, OSError) as e:
            print(f"Could not reach receiver {host}:{args.port} -> {e}")
            print("Is the receiver running and the port open in the firewall?")
            sys.exit(1)


if __name__ == "__main__":
    main()

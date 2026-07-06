"""Minimal CDP client over stdlib: drive chrome-headless-shell for UI verification."""
import base64, hashlib, json, os, socket, struct, subprocess, time, urllib.request

SHELL = os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-mac-arm64/chrome-headless-shell")

class WS:
    def __init__(self, url):
        host_port, path = url.split("://", 1)[1].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp: resp += self.sock.recv(4096)
        assert b"101" in resp.split(b"\r\n")[0], resp
        self.buf = b""
    def send(self, payload: str):
        data = payload.encode(); mask = os.urandom(4)
        header = b"\x81"
        n = len(data)
        if n < 126: header += bytes([0x80 | n])
        elif n < 65536: header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else: header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)
    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk: raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    def recv(self) -> str:
        payload = b""
        while True:
            h = self._read_exact(2)
            fin, opcode = h[0] & 0x80, h[0] & 0x0F
            n = h[1] & 0x7F
            if n == 126: n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read_exact(8))[0]
            frame = self._read_exact(n)
            if opcode == 0x9:  # ping -> pong
                self.send_pong(frame); continue
            payload += frame
            if fin: return payload.decode()
    def send_pong(self, data):
        mask = os.urandom(4)
        self.sock.sendall(b"\x8a" + bytes([0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

class Browser:
    def __init__(self, port=9333):
        self.proc = subprocess.Popen([SHELL, "--headless", f"--remote-debugging-port={port}",
                                      "--no-first-run", "--window-size=1500,950", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception: time.sleep(0.2)
        self.ws = WS(page["webSocketDebuggerUrl"])
        self.mid = 0
        self.cmd("Page.enable"); self.cmd("Runtime.enable")
        self.console = []
    def cmd(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.consoleAPICalled":
                self.console.append(msg["params"]); continue
            if msg.get("id") == self.mid:
                return msg.get("result", {})
    def goto(self, url, settle=2.0):
        self.cmd("Page.navigate", url=url); time.sleep(settle)
    def js(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")
    def mouse_move(self, x, y):
        self.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    def click(self, x, y):
        self.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        self.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
    def shot(self, path):
        data = self.cmd("Page.captureScreenshot", format="png")["data"]
        open(path, "wb").write(base64.b64decode(data))
    def close(self):
        try: self.proc.terminate()
        except Exception: pass

#!/usr/bin/env python3
"""Tiny stdlib-only Chrome DevTools Protocol probe for local UI checks.

Public surface:
  Browser(port)
  Browser.goto(url, settle)
  Browser.js(expr)
  Browser.mouse_move(x, y)
  Browser.click(x, y)
  Browser.cmd(method, **params)
  Browser.shot(path)
  Browser.console
  Browser.close()
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class WS:
    def __init__(self, url: str) -> None:
        host_port, path = url.split("://", 1)[1].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {resp!r}")
        self.buf = b""

    def send(self, payload: str) -> None:
        data = payload.encode()
        mask = os.urandom(4)
        header = b"\x81"
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def recv(self) -> str:
        payload = b""
        while True:
            h = self._read_exact(2)
            fin = h[0] & 0x80
            opcode = h[0] & 0x0F
            n = h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            frame = self._read_exact(n)
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed")
            if opcode == 0x9:
                self.send_pong(frame)
                continue
            if opcode in (0x1, 0x0):
                payload += frame
            if fin:
                return payload.decode()

    def send_pong(self, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(b"\x8a" + bytes([0x80 | len(data)]) + mask + masked)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class Browser:
    def __init__(self, port: int = 9333) -> None:
        self.port = port
        self.chrome = resolve_browser_binary()
        self.profile = tempfile.TemporaryDirectory(prefix="datagraph-ui-probe-")
        self.proc = subprocess.Popen(
            [
                self.chrome,
                "--headless",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile.name}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--disable-gpu",
                "--window-size=1500,950",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        page = self._wait_for_page()
        self.ws = WS(page["webSocketDebuggerUrl"])
        self.mid = 0
        self.console: list[dict[str, Any]] = []
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        self.cmd("Log.enable")

    def cmd(self, method: str, **params: Any) -> dict[str, Any]:
        self.mid += 1
        current = self.mid
        self.ws.send(json.dumps({"id": current, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            self._capture_event(msg)
            if msg.get("id") == current:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    def goto(self, url: str, settle: float = 2.0) -> None:
        self.cmd("Page.navigate", url=url)
        time.sleep(settle)

    def js(self, expr: str) -> Any:
        result = self.cmd(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=True,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JavaScript evaluation failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def mouse_move(self, x: float, y: float) -> None:
        self.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)

    def click(self, x: float, y: float) -> None:
        self.cmd(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )
        self.cmd(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )

    def shot(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self.cmd("Page.captureScreenshot", format="png")["data"]
        target.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ws.close()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.profile.cleanup()

    def _wait_for_page(self) -> dict[str, Any]:
        deadline = time.time() + 15
        url = f"http://127.0.0.1:{self.port}/json/list"
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    targets = json.load(response)
                return next(target for target in targets if target.get("type") == "page")
            except (StopIteration, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
                last_error = error
                time.sleep(0.2)
        self.close()
        raise RuntimeError(f"Chrome did not expose a CDP page on port {self.port}: {last_error}")

    def _capture_event(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(str(arg.get("value", arg.get("description", ""))) for arg in args)
            self.console.append({"type": params.get("type"), "text": text, "raw": params})
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            description = details.get("exception", {}).get("description", "")
            self.console.append(
                {
                    "type": "error",
                    "text": details.get("text") or description,
                    "raw": params,
                }
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self.console.append(
                {"type": entry.get("level"), "text": entry.get("text", ""), "raw": params}
            )


def resolve_browser_binary() -> str:
    tried: list[str] = []
    env = os.environ.get("DATAGRAPH_CHROME_BIN")
    if env:
        tried.append(f"DATAGRAPH_CHROME_BIN={env}")
        if _is_executable(Path(env)):
            return env

    for candidate in _playwright_headless_shells():
        tried.append(str(candidate))
        if _is_executable(candidate):
            return str(candidate)

    for name in ("google-chrome", "chromium"):
        tried.append(f"{name} on PATH")
        resolved = shutil.which(name)
        if resolved:
            return resolved

    raise RuntimeError(
        "Could not find a Chromium browser for UI probing. Tried:\n"
        + "\n".join(f"  - {item}" for item in tried)
    )


def _playwright_headless_shells() -> list[Path]:
    base = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = list(base.glob("chromium_headless_shell-*/**/chrome-headless-shell"))
    return sorted(candidates, key=_playwright_revision, reverse=True)


def _playwright_revision(path: Path) -> int:
    match = re.search(r"chromium_headless_shell-(\d+)", str(path))
    return int(match.group(1)) if match else -1


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)

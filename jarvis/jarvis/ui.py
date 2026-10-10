"""Jarvis app window: a local web UI with the orb, conversation and approvals.

The assistant pushes events (state, mic level, what you said, tools, answers) to the
page over Server-Sent Events; the page sends back commands (tap to talk, typed text,
approve/deny). Only 127.0.0.1, and every request needs the session token.

Opened as a native window with pywebview when installed, otherwise as an app-style
Edge/Chrome window, otherwise in the default browser.
"""

from __future__ import annotations

import hmac
import json
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PAGE = Path(__file__).with_name("ui.html")


class NullUI:
    """Used with --no-ui: same interface, does nothing."""

    def emit(self, kind: str, **data: Any) -> None:
        pass

    def next_command(self, timeout: float = 0) -> tuple[str, str] | None:
        return None

    def has_command(self) -> bool:
        return False

    def close(self) -> None:
        pass


class UI(NullUI):
    def __init__(self, store, *, port: int = 0, token: str | None = None):
        self.store = store
        self.token = token or secrets.token_urlsafe(18)
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue()
        self._last_state: dict[str, Any] = {"kind": "state", "state": "sleeping"}
        self._last_level = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _handler(self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    # ---------------------------------------------------------------- events
    def emit(self, kind: str, **data: Any) -> None:
        if kind == "level":  # mic level: at most ~15 updates/s
            now = time.monotonic()
            if now - self._last_level < 0.066:
                return
            self._last_level = now
        event = {"kind": kind, "ts": time.time(), **data}
        if kind == "state":
            self._last_state = event
        with self._lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(event)
                except queue.Full:  # a stuck page must never block the assistant
                    pass

    def _subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        q.put_nowait(self._last_state)
        with self._lock:
            self._clients.append(q)
        return q

    def _unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    # -------------------------------------------------------------- commands
    def push_command(self, kind: str, payload: str = "") -> None:
        self._commands.put((kind, payload))

    def next_command(self, timeout: float = 0) -> tuple[str, str] | None:
        try:
            return self._commands.get(timeout=timeout) if timeout else self._commands.get_nowait()
        except queue.Empty:
            return None

    def has_command(self) -> bool:
        return not self._commands.empty()

    def close(self) -> None:
        self.server.shutdown()

    # ---------------------------------------------------------------- window
    def open_window(self, *, title: str = "Jarvis") -> None:
        """Open the app window without blocking. Best available option wins."""
        if _try_app_browser(self.url):
            return
        webbrowser.open(self.url)


def _try_app_browser(url: str) -> bool:
    """Chromeless window in Edge or Chrome (installed on most Windows PCs)."""
    candidates = []
    if sys.platform == "win32":
        import os

        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                           Path(base) / "Google/Chrome/Application/chrome.exe"]
    candidates += [Path(p) for p in filter(None, (shutil.which("msedge"), shutil.which("google-chrome"),
                                                  shutil.which("chromium")))]
    for exe in candidates:
        if exe.is_file():
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=440,820"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


def run_native_window(ui: UI, *, on_closed=None) -> bool:
    """Blocks in a native pywebview window (must run on the main thread). False if unavailable."""
    try:
        import webview  # pywebview
    except ImportError:
        return False
    webview.create_window("Jarvis", ui.url, width=440, height=820, background_color="#05060f",
                          min_size=(360, 560))
    webview.start()
    if on_closed:
        on_closed()
    return True


def _handler(ui: UI):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _ok_token(self) -> bool:
            supplied = self.headers.get("X-Jarvis-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return hmac.compare_digest(supplied, ui.token)

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ok_token():
                return self._send(403, b"forbidden", "text/plain")
            route = urlparse(self.path).path
            if route == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if route == "/events":
                return self._stream()
            self._send(404, b"not found", "text/plain")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = ui._subscribe()
            try:
                for item in ui.store.pending_approvals():  # approvals waiting before the page opened
                    q.put_nowait({"kind": "approval", "id": item["id"], "action": item["action"],
                                  "args": item["args"]})
                while True:
                    try:
                        event = q.get(timeout=15)
                        chunk = f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    except queue.Empty:
                        chunk = ": keep-alive\n\n"
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ui._unsubscribe(q)

        def do_POST(self):
            # Header token only: other web pages can't set it, so they can't drive Jarvis.
            if not hmac.compare_digest(self.headers.get("X-Jarvis-Token", ""), ui.token):
                return self._send(403, b'{"error":"forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b'{"error":"bad json"}')
            route = urlparse(self.path).path
            if route == "/api/talk":
                ui.push_command("talk")
            elif route == "/api/text":
                text = str(body.get("text", "")).strip()[:2000]
                if not text:
                    return self._send(400, b'{"error":"empty"}')
                ui.push_command("text", text)
            elif route == "/api/approve":
                ok = ui.store.decide(str(body.get("id", "")), bool(body.get("approve")))
                if ok:
                    ui.store.log("human", "clicked", f"user {'approved' if body.get('approve') else 'rejected'} "
                                 f"#{body.get('id')} in the app", request_id=body.get("id"))
                ui.emit("approval_done", id=body.get("id"))
                return self._send(200, json.dumps({"ok": ok}).encode())
            else:
                return self._send(404, b'{"error":"not found"}')
            self._send(200, b'{"ok":true}')

    return Handler

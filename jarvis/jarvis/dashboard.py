"""Live dashboard: watch the pipeline, approve/reject dangerous actions, kill switch.

  python -m jarvis.dashboard                 # http://127.0.0.1:8765/?token=...
  python -m jarvis.dashboard --host 0.0.0.0  # reachable from your phone over Tailscale

Every request needs the secret token (printed at startup, kept in
JARVIS_HOME/dashboard_token), so other devices or web pages can't approve actions.
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .store import Store

PAGE = Path(__file__).with_name("dashboard.html")


def load_token(settings: config.Settings) -> str:
    path = settings.home / "dashboard_token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(24), "utf-8")
    return path.read_text("utf-8").strip()


def make_handler(settings: config.Settings, store: Store, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # keep the console quiet
            pass

        def _authorized(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Jarvis-Token") or query.get("token", [""])[0]
            return hmac.compare_digest(supplied, token)

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def do_GET(self):
            if not self._authorized():
                return self._send(403, b"forbidden: open the link printed by the dashboard", "text/plain")
            route = urlparse(self.path).path
            if route == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if route == "/api/state":
                after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
                store.expire_stale(settings.approval_wait_s)
                return self._json({"events": store.events(after_id=after),
                                   "pending": store.pending_approvals(),
                                   "paused": settings.paused})
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # POSTs need the header (not just the query string), which a
            # cross-site form or <img> on another web page cannot set.
            if not hmac.compare_digest(self.headers.get("X-Jarvis-Token", ""), token):
                return self._json({"error": "forbidden"}, 403)
            route = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if route in ("/api/approve", "/api/reject"):
                store.expire_stale(settings.approval_wait_s)
                approved = route.endswith("approve")
                ok = store.decide(str(body.get("id", "")), approved)
                if ok:
                    store.log("human", "clicked", f"user {'approved' if approved else 'rejected'} "
                              f"#{body['id']}", request_id=body["id"])
                return self._json({"ok": ok})
            if route == "/api/pause":
                settings.pause_flag.touch()
                store.log("human", "paused", "kill switch ON — only read actions allowed")
                return self._json({"paused": True})
            if route == "/api/resume":
                settings.pause_flag.unlink(missing_ok=True)
                store.log("human", "resumed", "kill switch OFF")
                return self._json({"paused": False})
            self._json({"error": "not found"}, 404)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    settings = config.load()
    store = Store(settings.db_path)
    token = load_token(settings)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(settings, store, token))
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Jarvis dashboard: http://{shown}:{args.port}/?token={token}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

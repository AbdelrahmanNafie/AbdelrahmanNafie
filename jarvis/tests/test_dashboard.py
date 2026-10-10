import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jarvis.dashboard import load_token, make_handler


@pytest.fixture
def server(settings, store):
    token = load_token(settings)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings, store, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Jarvis-Token": token} if token else {})
    return json.loads(urllib.request.urlopen(req).read())


def test_requires_token(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/state")
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        _post(base + "/api/pause", {})


def test_query_token_cannot_approve(server, store):
    base, token = server
    req_id = store.create_approval("delete_file", {"path": "x"})
    with pytest.raises(urllib.error.HTTPError):
        _post(f"{base}/api/approve?token={token}", {"id": req_id})
    assert store.approval_status(req_id) == "pending"


def test_approve_and_kill_switch(server, store, settings):
    base, token = server
    req_id = store.create_approval("delete_file", {"path": "x"})
    state = json.loads(urllib.request.urlopen(f"{base}/api/state?token={token}").read())
    assert state["pending"][0]["id"] == req_id
    assert _post(base + "/api/approve", {"id": req_id}, token) == {"ok": True}
    assert store.approval_status(req_id) == "approved"
    assert _post(base + "/api/pause", {}, token) == {"paused": True} and settings.paused
    assert _post(base + "/api/resume", {}, token) == {"paused": False} and not settings.paused

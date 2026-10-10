import json
import threading
import urllib.error
import urllib.request

import pytest
from google.genai import types

from jarvis import screen
from jarvis.gateway import Gateway
from jarvis.quick import QuickBrain
from jarvis.ui import UI


@pytest.fixture
def ui(store):
    u = UI(store)
    yield u
    u.close()


def _post(u, path, body, token=True):
    req = urllib.request.Request(f"http://127.0.0.1:{u.port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Jarvis-Token": u.token} if token else {})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def test_page_needs_token(ui):
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/", timeout=5)
    assert b"orb" in urllib.request.urlopen(ui.url, timeout=5).read()


def test_commands_from_the_window(ui):
    _post(ui, "/api/talk", {})
    _post(ui, "/api/text", {"text": "افتح كروم"})
    assert ui.next_command() == ("talk", "")
    assert ui.next_command() == ("text", "افتح كروم")
    assert ui.next_command() is None
    with pytest.raises(urllib.error.HTTPError):
        _post(ui, "/api/talk", {}, token=False)  # other web pages can't drive Jarvis


def test_events_stream_and_approval_from_window(ui, store):
    req_id = store.create_approval("close_app", {"name": "chrome"})
    got = []

    def read():
        resp = urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/events?token={ui.token}", timeout=5)
        for line in resp:
            if line.startswith(b"data: "):
                got.append(json.loads(line[6:]))
            if len(got) >= 3:
                return

    t = threading.Thread(target=read, daemon=True)
    t.start()
    import time
    time.sleep(0.3)
    ui.emit("heard", text="hello")
    t.join(3)
    kinds = [e["kind"] for e in got]
    assert kinds[:3] == ["state", "approval", "heard"]  # current state, waiting approval, live event
    assert _post(ui, "/api/approve", {"id": req_id, "approve": True}) == {"ok": True}
    assert store.approval_status(req_id) == "approved"


def test_level_events_are_throttled(ui):
    q = ui._subscribe()
    q.get_nowait()  # initial state
    for _ in range(50):
        ui.emit("level", value=0.5)
    assert q.qsize() <= 2


def test_capture_downscales_to_jpeg():
    from PIL import Image
    shot = screen.capture(grab=lambda: Image.new("RGB", (3840, 2160), "navy"))
    img = Image.open(__import__("io").BytesIO(shot))
    assert img.format == "JPEG" and max(img.size) == screen.MAX_SIDE


def _call(name, args):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))])


def _text(t):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=t)]))])


def test_brain_sees_window_title_and_screenshot(settings, store):
    seen = []

    class Fake:
        models = None

        def __init__(self):
            self.models = self
            self.step = 0

        def generate_content(self, *, model, contents, config):
            seen.append(contents)
            self.step += 1
            return _call("look_at_screen", {"question": "what is this"}) if self.step == 1 else \
                _text("HEARD: ايه ده\nIt's a Python error about a missing module.")

    events = []
    qb = QuickBrain(settings, Gateway(settings, store), client=Fake(), models=["m"],
                    capture_screen=lambda: b"\xff\xd8JPEG", active_window=lambda: "main.py - VS Code",
                    on_event=lambda kind, **d: events.append(kind))
    said, answer = qb.handle(audio=b"RIFF")
    assert "missing module" in answer
    first_text = " ".join(p.text or "" for p in seen[0][0].parts)
    assert "main.py - VS Code" in first_text
    images = [p for c in seen[1] for p in c.parts if p.inline_data and p.inline_data.mime_type == "image/jpeg"]
    assert len(images) == 1
    assert events == ["turn", "tool", "screen"]
    # the screenshot is not kept in history
    assert not any(p.inline_data for t in qb.turns for c in t for p in c.parts)

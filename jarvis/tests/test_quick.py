"""Quick brain: our own tool loop over the real google-genai types, with Google's replies faked."""
import pytest
from google import genai
from google.genai import errors, types

from jarvis.ears import Heard
from jarvis.gateway import Gateway
from jarvis.quick import QuickBrain, QuickError


def call(name, args):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))])


def text(t):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=t)]))])


def last_result(contents):
    return [p.function_response.response for c in contents for p in (c.parts or []) if p.function_response][-1]["result"]


def quota(model):
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                              "message": f"Quota exceeded, model: {model}. Please retry in 3.2s."}})


class FakeGemini:
    """Plays a script of replies; each step can raise to simulate busy models."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []  # (model, contents)
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.requests.append((model, list(contents)))
        step = self.script.pop(0)
        return step(model, contents)


HEARD = Heard(original="افتح التقرير النهائي", language="egyptian_arabic", english="Open the final report")


def brain(settings, gw, fake, **kw):
    return QuickBrain(settings, gw, client=fake, models=["fast", "backup"], sleep=lambda s: None, **kw)


def test_finds_and_opens_a_file(settings, store):
    (settings.code_roots[0] / "report_final.pdf").write_text("x")
    opened = []
    fake = FakeGemini([
        lambda m, c: call("search_files", {"query": "report final"}),
        lambda m, c: call("open_file_or_folder", {"path": last_result(c)["result"]["results"][0]["path"]}),
        lambda m, c: text("HEARD: افتح التقرير النهائي\nI opened your final report."),
    ])
    said, answer = brain(settings, Gateway(settings, store, opener=opened.append), fake).handle(HEARD)
    assert (said, answer) == ("افتح التقرير النهائي", "I opened your final report.")
    assert opened[0].endswith("report_final.pdf")


def test_quota_error_switches_model_without_repeating_actions(settings, store):
    """The bug from the user's log: a 429 after an action already ran."""
    launched = []
    fake = FakeGemini([
        lambda m, c: call("open_app", {"name": "notepad"}),
        lambda m, c: (_ for _ in ()).throw(quota(m)),      # "fast" is out of quota after the action
        lambda m, c: text("HEARD: open notepad\nNotepad is open."),  # "backup" finishes the turn
    ])
    said, answer = brain(settings, Gateway(settings, store, launcher=launched.append), fake).handle(HEARD)
    assert answer == "Notepad is open."
    assert launched == [["notepad.exe"]]                     # ran exactly once
    assert [m for m, _ in fake.requests] == ["fast", "fast", "backup"]


def test_all_models_out_of_quota_waits_once_then_explains(settings, store):
    waits = []
    fake = FakeGemini([lambda m, c: (_ for _ in ()).throw(quota(m))] * 4)
    qb = QuickBrain(settings, Gateway(settings, store), client=fake, models=["a", "b"], sleep=waits.append)
    with pytest.raises(QuickError, match="free tier"):
        qb.handle(HEARD)
    assert waits == [3.7] and len(fake.requests) == 4


def test_bad_key_fails_fast_without_switching(settings, store):
    bad = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid"}})
    fake = FakeGemini([lambda m, c: (_ for _ in ()).throw(bad)])
    with pytest.raises(QuickError, match="400"):
        brain(settings, Gateway(settings, store), fake).handle(HEARD)
    assert len(fake.requests) == 1


def test_audio_goes_in_once_and_history_keeps_only_text(settings, store):
    fake = FakeGemini([
        lambda m, c: text("HEARD: اكتب نوت\nDone."),
        lambda m, c: text("HEARD: افتحها\nOpened."),
    ])
    qb = brain(settings, Gateway(settings, store), fake)
    assert qb.handle(audio=b"RIFF....") == ("اكتب نوت", "Done.")
    qb.handle(audio=b"RIFF2...")
    second_request = fake.requests[1][1]
    audio_parts = [p for c in second_request for p in c.parts if p.inline_data]
    assert len(audio_parts) == 1  # only the new recording; the old one became text
    assert any("اكتب نوت" in (p.text or "") for c in second_request for p in c.parts)


def test_denials_reach_the_model(settings, store):
    settings.pause_flag.touch()
    seen = {}

    def step2(m, c):
        seen["r"] = last_result(c)
        return text("HEARD: x\nJarvis is paused.")

    fake = FakeGemini([lambda m, c: call("open_website", {"url": "https://example.com"}), step2])
    brain(settings, Gateway(settings, store, opener=lambda u: pytest.fail("opened")), fake).handle(HEARD)
    assert seen["r"]["status"] == "denied"


def test_heavy_work_goes_to_claude(settings, store):
    asked = []
    fake = FakeGemini([lambda m, c: call("ask_claude", {"task": "Plan my week"}),
                       lambda m, c: text("HEARD: x\n" + last_result(c)["claude_reply"])])
    qb = brain(settings, Gateway(settings, store), fake, ask_claude=lambda t: asked.append(t) or "Here is your plan.")
    assert qb.handle(HEARD)[1] == "Here is your plan." and asked == ["Plan my week"]


def test_every_tool_is_declared(settings, store):
    qb = brain(settings, Gateway(settings, store), FakeGemini([]), ask_claude=lambda t: "")
    assert {"open_app", "open_website", "google_search", "read_web_page", "search_files", "open_file_or_folder",
            "draft_message", "code_task", "ask_claude", "delete_file", "media_control", "lock_screen",
            "list_running_apps", "close_app", "read_clipboard", "copy_to_clipboard", "set_reminder"} <= set(qb.tools)
    # Gemini can build a schema for every tool (catches string-annotation bugs).
    client = genai.Client(api_key="fake")
    for fn in qb.tools.values():
        types.FunctionDeclaration.from_callable(client=client._api_client, callable=fn)

"""Runs Gemini's real function-calling loop (google-genai) with only the server reply faked."""
import pytest
from google import genai
from google.genai import types

from jarvis.ears import Heard
from jarvis.gateway import Gateway
from jarvis.quick import QuickBrain


def _call(name, args):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))])


def _text(t):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=t)]))])


def _last_tool_result(contents):
    resp = [p.function_response.response for c in contents for p in (c.parts or []) if p.function_response][-1]
    return resp["result"]


@pytest.fixture
def client():
    return genai.Client(api_key="fake-key")


HEARD = Heard(original="افتح التقرير النهائي", language="egyptian_arabic", english="Open the final report")


def test_gemini_finds_and_opens_a_file(settings, store, client):
    (settings.code_roots[0] / "report_final.pdf").write_text("x")
    opened = []
    gw = Gateway(settings, store, opener=opened.append)
    script = iter([
        lambda c: _call("search_files", {"query": "report final"}),
        lambda c: _call("open_file_or_folder", {"path": _last_tool_result(c)["result"]["results"][0]["path"]}),
        lambda c: _text("I opened your final report."),
    ])
    client.models._generate_content = lambda *, model, contents, config: next(script)(contents)
    brain = QuickBrain(settings, gw, client=client)
    assert brain.handle(HEARD) == "I opened your final report."
    assert opened[0].endswith("report_final.pdf") and brain.calls == 2


def test_gemini_sees_denials_and_reports_them(settings, store, client):
    settings.pause_flag.touch()  # kill switch: only reads allowed
    gw = Gateway(settings, store, opener=lambda u: pytest.fail("must not open"))
    seen = {}

    def step2(contents):
        seen["result"] = _last_tool_result(contents)
        return _text("Jarvis is paused, so I couldn't open it.")

    script = iter([lambda c: _call("open_website", {"url": "https://example.com"}), step2])
    client.models._generate_content = lambda *, model, contents, config: next(script)(contents)
    assert "paused" in QuickBrain(settings, gw, client=client).handle(HEARD)
    assert seen["result"]["status"] == "denied"


def test_heavy_work_is_handed_to_claude(settings, store, client):
    asked = []
    script = iter([lambda c: _call("ask_claude", {"task": "Plan my week"}),
                   lambda c: _text(_last_tool_result(c)["claude_reply"])])
    client.models._generate_content = lambda *, model, contents, config: next(script)(contents)
    brain = QuickBrain(settings, Gateway(settings, store), client=client,
                       ask_claude=lambda t: asked.append(t) or "Here is your plan.")
    assert brain.handle(HEARD) == "Here is your plan." and asked == ["Plan my week"]


def test_gemini_gets_every_tool(settings, store, client):
    brain = QuickBrain(settings, Gateway(settings, store), client=client, ask_claude=lambda t: "")
    names = {t.__name__ for t in brain.chat._config.tools}
    assert {"open_app", "open_website", "google_search", "read_web_page", "search_files",
            "open_file_or_folder", "draft_message", "code_task", "ask_claude", "delete_file"} <= names

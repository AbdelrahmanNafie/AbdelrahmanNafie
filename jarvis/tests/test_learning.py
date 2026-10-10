"""Consistency and learning: standing instructions, restored history, repeated openings,
activity tracking / workflow learning, faster replies (fewer round trips, streamed voice)."""

import time
from types import SimpleNamespace

import numpy as np
from google.genai import types

from jarvis import activity, assistant, profile, quick, voice
from jarvis.gateway import Gateway
from jarvis.quick import QuickBrain


def text(t):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=t)]))])


def text_and_calls(t, *calls):
    parts = [types.Part(text=t)] + [types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in calls]
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


class FakeGemini:
    def __init__(self, script):
        self.script, self.requests, self.models = list(script), [], self

    def generate_content(self, *, model, contents, config):
        self.requests.append((model, list(contents), config))
        return self.script.pop(0)


def brain(settings, store, script):
    fake = FakeGemini(script)
    return QuickBrain(settings, Gateway(settings, store), client=fake, models=["m"], sleep=lambda s: None,
                      active_window=lambda: ""), fake


# ------------------------------------------------------------ instructions
def test_rule_detection():
    assert quick.looks_like_rule("From now on, always answer me in Arabic")
    assert quick.looks_like_rule("next time, save my notes in the Projects folder")
    assert quick.looks_like_rule("من دلوقتي افتح كروم مش ايدج")
    assert not quick.looks_like_rule("never mind")
    assert not quick.looks_like_rule("is it always this hot in Cairo?")
    assert not quick.looks_like_rule("open youtube")


def test_rule_is_saved_even_if_the_model_forgets(settings, store):
    qb, _ = brain(settings, store, [text("HEARD: From now on call my reports summaries\nOkay, summaries it is.")])
    qb.handle(audio=b"RIFF")
    assert [m["text"] for m in store.memories() if m["category"] == "instructions"] == \
        ["From now on call my reports summaries"]


def test_remember_with_reply_takes_one_request(settings, store):
    qb, fake = brain(settings, store, [text_and_calls(
        "HEARD: Always use dark mode in my mockups\nNoted, dark mode from now on.",
        ("remember", {"text": "Use dark mode in mockups", "category": "instructions"}))])
    said, answer = qb.handle(audio=b"RIFF")
    assert answer == "Noted, dark mode from now on." and len(fake.requests) == 1
    rules = [m["text"] for m in store.memories() if m["category"] == "instructions"]
    assert rules == ["Use dark mode in mockups"]  # the model's version, not a duplicate of the raw words


def test_instructions_lead_the_prompt(store):
    store.remember("Builds Jarvis", "projects")
    store.remember("Answer with numbers first", "instructions")
    store.remember("Never open Edge, use Chrome", "instructions")
    text_ = quick.build_system_prompt(profile.Profile(), store.memories(), {}, [], "now", workflow="Codes at night")
    rules = text_.split("standing instructions")[1].split("Who you are")[0]
    assert rules.index("Answer with numbers first") < rules.index("Never open Edge")  # in the order given
    assert "Builds Jarvis" not in rules and "Codes at night" in text_


# ------------------------------------------------------------ continuity
def test_conversation_survives_a_restart(settings, store):
    qb, _ = brain(settings, store, [text("HEARD: my project is called Atlas\nNice name.")])
    qb.handle(audio=b"RIFF")
    qb2, fake2 = brain(settings, store, [text("HEARD: what's it called?\nAtlas.")])
    qb2.handle(audio=b"RIFF")
    sent = " ".join(p.text or "" for c in fake2.requests[0][1] for p in c.parts)
    assert "my project is called Atlas" in sent and "Nice name." in sent


def test_repeated_opening_is_dropped():
    prev = ["Hey Abdelrahman! Chrome is open."]
    assert quick.drop_repeated_opening("Hey Abdelrahman! The weather is sunny.", prev) == "The weather is sunny."
    assert quick.drop_repeated_opening("Hey Abdelrahman!", prev) == "Hey Abdelrahman!"  # nothing else to say
    assert quick.drop_repeated_opening("The weather is sunny.", prev) == "The weather is sunny."


def test_later_steps_think_less(settings, store):
    qb, fake = brain(settings, store, [
        text_and_calls("", ("system_info", {})),
        text("HEARD: what time is it\nIt's 9 pm."),
    ])
    qb.handle(audio=b"RIFF")
    first, second = (r[2].thinking_config.thinking_level for r in fake.requests)
    assert first == types.ThinkingLevel.LOW and second == types.ThinkingLevel.MINIMAL
    assert fake.requests[0][2].temperature <= 0.5


# ------------------------------------------------------------ activity
def test_tracker_merges_masks_and_respects_off_and_away(store):
    now = [1_000_000.0]
    fg = [("Code", "quick.py - jarvis - Visual Studio Code")]
    away = [0.0]
    on = [True]
    t = activity.Tracker(store, sample=lambda: fg[0], idle=lambda: away[0], enabled=lambda: on[0])
    for _ in range(4):
        t.tick(now[0])
        now[0] += 15
    fg[0] = ("chrome", "Bank login - InPrivate - Microsoft Edge")
    t.tick(now[0])
    rows = store.activity(0, now[0] + 1)
    assert [(r["app"], r["title"]) for r in rows] == [("VS Code", "quick.py - jarvis - Visual Studio Code"),
                                                      ("Chrome", "(private)")]
    assert rows[0]["end_ts"] - rows[0]["start_ts"] == 45
    away[0] = 600
    assert t.tick(now[0] + 15) is False
    away[0], on[0] = 0, False
    assert t.tick(now[0] + 30) is False


def test_summary_and_today_line(store):
    now = time.time()
    store.log_activity("VS Code", "quick.py - jarvis", now - 3600)
    store.log_activity("VS Code", "quick.py - jarvis", now - 60, max_gap_s=99999)
    s = activity.report(store, 1, now)
    assert s["apps"][0]["app"] == "VS Code" and 58 <= s["apps"][0]["minutes"] <= 60
    assert "VS Code" in activity.today_line(store, now) or time.localtime(now).tm_hour == 0


def test_my_activity_tool(settings, store):
    store.log_activity("Figma", "Jarvis app - Figma", time.time() - 120)
    r = Gateway(settings, store).request("my_activity", {"days": 1})
    assert r["status"] == "ok" and r["result"]["untrusted_activity"]["apps"][0]["app"] == "Figma"
    assert Gateway(settings, store).request("my_activity", {"days": 999})["status"] == "error"


def test_workflow_is_learned_once_a_day(settings, store):
    from jarvis.ui import NullUI

    now = time.time()
    store.log_activity("VS Code", "quick.py - jarvis", now - 7200)
    store.log_activity("VS Code", "quick.py - jarvis", now - 60, max_gap_s=99999)
    asked = []
    m = assistant.Monitor(settings, store, NullUI(), battery=lambda: None,
                          generate=lambda prompt: asked.append(prompt) or "Builds Jarvis in VS Code")
    assert m.maybe_learn(now) is True and "VS Code" in asked[0]
    assert store.get("workflow")[0] == "Builds Jarvis in VS Code"
    assert m.maybe_learn(now + 7200) is False  # already fresh
    profile.Profile(activity_tracking=False).save(settings.home)
    m._last_learn_try = 0
    assert m.maybe_learn(now + 2 * 86400) is False


def test_activity_tracking_can_be_turned_off_by_voice(settings, store):
    Gateway(settings, store).request("set_preference", {"key": "activity tracking", "value": "off"})
    assert profile.Profile.load(settings.home).activity_tracking is False


# ------------------------------------------------------------ voice speed
class FakeStream:
    def __init__(self, written):
        self.written = written

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def write(self, chunk):
        self.written.append(len(chunk))


def test_voice_streams_and_never_sends_a_style_prefix(monkeypatch):
    written, asked = [], []
    sd = SimpleNamespace(OutputStream=lambda **kw: FakeStream(written))
    monkeypatch.setattr(voice, "_sounddevice", lambda: sd)
    pcm = (np.ones(24_000, dtype=np.int16) * 1000).tobytes()  # 1 s of audio in 4 chunks

    def stream(*, model, contents, config):
        asked.append(contents)
        for i in range(4):
            part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm[i * 12000:(i + 1) * 12000]))
            yield SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    levels = []
    client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream))
    spk = voice.Speaker("gemini", client=client, on_level=levels.append)
    spk.say("Chrome is open.")
    assert asked == ["Chrome is open."]  # nothing that could be read aloud in front of the reply
    assert sum(written) == 24_000 and spk.first_audio_s is not None and max(levels) > 0

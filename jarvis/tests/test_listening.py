"""Listening to the right person, being interruptible, one consistent voice, background research."""

import hashlib
from types import SimpleNamespace

import numpy as np
import pytest
from google.genai import types

from jarvis import assistant, profile, speaker_id, voice
from jarvis.gateway import Gateway
from jarvis.quick import QuickBrain


def tone(rms: float, n: int = voice.CHUNK) -> np.ndarray:
    return np.full(n, int(rms), dtype=np.int16)


# ---------------------------------------------------------------- noise vs speech
def test_loud_noise_that_isnt_speech_is_ignored():
    d = voice.SilenceDetector(noise_floor=100, no_speech_s=0.8)
    assert not any(d.feed(5000, speech=False) for _ in range(5))
    assert not d.heard_speech
    d.feed(5000, speech=True)
    assert d.heard_speech


# ---------------------------------------------------------------- barge-in
def _warm(det, out=3000, ratio=0.3, n=8):
    for _ in range(n):
        assert det.feed(tone(out * ratio), out_level=out, speech=True) is False


def test_own_voice_echo_does_not_interrupt():
    det = voice.BargeDetector(noise_floor=100)
    _warm(det)
    assert not any(det.feed(tone(900), out_level=3000, speech=True) for _ in range(20))


def test_talking_over_jarvis_interrupts_and_keeps_the_start_of_the_words():
    det = voice.BargeDetector(noise_floor=100)
    _warm(det)
    results = [det.feed(tone(4000), out_level=3000, speech=True) for _ in range(3)]
    assert results == [False, False, True] and det.reason == "voice"
    assert len(det.candidate) >= 3  # pre-roll included


def test_noise_or_someone_else_does_not_interrupt_with_a_voiceprint():
    det = voice.BargeDetector(noise_floor=100, verify=lambda s: False, verify_chunks=4)
    assert not any(det.feed(tone(4000), out_level=0, speech=True) for _ in range(20))
    me = voice.BargeDetector(noise_floor=100, verify=lambda s: True, verify_chunks=4)
    assert any(me.feed(tone(4000), out_level=0, speech=True) for _ in range(8)) and me.reason == "your voice"
    assert not any(voice.BargeDetector(noise_floor=100).feed(tone(4000), out_level=0, speech=False)
                   for _ in range(20))


def test_wake_word_always_interrupts():
    det = voice.BargeDetector(energy=False, wake_score=lambda c: 0.9)
    assert det.feed(tone(10), out_level=3000) and det.reason == "wake word"


# ---------------------------------------------------------------- speaker
class FakeOut:
    def __init__(self, log):
        self.log = log

    def start(self):
        pass

    def write(self, chunk):
        self.log.append(len(chunk))

    def abort(self):
        self.log.append("abort")

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_audio(monkeypatch):
    log = []
    monkeypatch.setattr(voice, "_sounddevice", lambda: SimpleNamespace(OutputStream=lambda **kw: FakeOut(log)))
    return log


def test_speaking_stops_the_moment_you_interrupt(fake_audio, monkeypatch):
    monkeypatch.setattr(voice, "_edge_available", lambda: True)
    spk = voice.Speaker("edge")
    spk._edge_synth = lambda text: (np.ones(24_000 * 3, dtype=np.int16) * 500, 24_000)  # 3 s
    blocks = iter(range(1000))
    spoken = spk.say("This is a long explanation that goes on and on about many things.",
                     should_stop=lambda: next(blocks) >= 10)
    assert spoken.interrupted and 0.4 < spoken.played_s < 0.6
    assert "abort" in fake_audio and spoken.heard_text.endswith("…")


def test_edge_voice_speaks_sentence_by_sentence_and_caches_short_phrases(fake_audio, monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "_edge_available", lambda: True)
    asked = []
    spk = voice.Speaker("edge", cache_dir=tmp_path)
    spk._edge_synth = lambda text: asked.append(text) or (np.ones(2400, dtype=np.int16), 24_000)
    spk.say("Here's what I found about the release. It ships next week with three new features.")
    assert len(asked) == 2 and spk.engine_used == "edge"
    spk.say("One sec, let me check.")
    spk.say("One sec, let me check.")
    assert asked.count("One sec, let me check.") == 1  # second time from the cache


def test_a_reply_never_switches_voice_halfway(fake_audio, monkeypatch):
    monkeypatch.setattr(voice, "_edge_available", lambda: True)
    windows = []
    spk = voice.Speaker("edge", runner=lambda cmd, **kw: windows.append(kw["input"]))
    calls = iter([True, False])

    def synth(text):
        if not next(calls):
            raise RuntimeError("network dropped")
        return np.ones(2400, dtype=np.int16), 24_000

    spk._edge_synth = synth
    spk.say("First sentence is long enough to stand alone. Second sentence fails to download sadly.")
    assert windows == []  # stopped quietly instead of repeating it all in another voice


def test_voice_falls_back_before_a_reply_starts(fake_audio, monkeypatch):
    monkeypatch.setattr(voice, "_edge_available", lambda: True)
    monkeypatch.setattr(voice.sys, "platform", "win32")
    windows = []
    broken = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: (_ for _ in ()).throw(
        RuntimeError("503"))))
    spk = voice.Speaker("edge", client=broken, runner=lambda cmd, **kw: windows.append(kw["input"]))
    spk._edge_synth = lambda text: (_ for _ in ()).throw(RuntimeError("offline"))
    spk._can_stream = False
    spk.say("Hello there.")
    assert windows == ["Hello there."]


def test_voice_names():
    assert voice.engine_for("Ava") == ("edge", "en-US-AvaMultilingualNeural")
    assert voice.engine_for("salma") == ("edge", "ar-EG-SalmaNeural")
    assert voice.engine_for("kore") == ("gemini", "Kore")
    assert voice.engine_for("windows") == ("windows", "")


def test_old_profiles_move_off_the_quota_limited_voice(settings):
    (settings.home / "profile.json").write_text('{"voice": "Aoede", "user_name": "Abdelrahman"}')
    p = profile.Profile.load(settings.home)
    assert p.voice == "Ava" and p.user_name == "Abdelrahman"
    p.voice = "Aoede"
    p.save(settings.home)
    assert profile.Profile.load(settings.home).voice == "Aoede"  # chosen on purpose: kept
    assert profile.apply(p, "voice", "salma").voice == "Salma"
    with pytest.raises(profile.PreferenceError):
        profile.apply(p, "voice", "robot9000")


# ---------------------------------------------------------------- voiceprint
class FakeEmbedder:
    """Embeds by 'pitch': chunks of value v map to a direction; other values are other people."""

    def __call__(self, samples):
        if len(samples) < 8000:
            return None
        v = float(np.median(np.abs(samples)))
        e = np.array([np.cos(v / 1000), np.sin(v / 1000), 0.1])
        return e / np.linalg.norm(e)


def test_voiceprint_accepts_you_and_rejects_others(settings):
    vp = speaker_id.VoicePrint(settings.home, embedder=FakeEmbedder(), threshold=0.9)
    me = [np.full(16_000, 1000, dtype=np.int16) for _ in range(3)]
    vp.enroll(me)
    assert speaker_id.VoicePrint(settings.home, embedder=FakeEmbedder()).enrolled  # saved
    assert vp.matches(np.full(32_000, 1000, dtype=np.int16))
    assert not vp.matches(np.full(32_000, 2600, dtype=np.int16))
    mixed = np.concatenate([np.full(48_000, 2600, dtype=np.int16), np.full(24_000, 1000, dtype=np.int16)])
    assert vp.matches(mixed)  # you spoke in part of it (others around you)
    with pytest.raises(ValueError):
        vp.enroll([np.full(100, 1000, dtype=np.int16)])


def test_not_enrolled_lets_everyone_through(settings):
    assert speaker_id.VoicePrint(settings.home, embedder=FakeEmbedder()).matches(np.zeros(16_000, dtype=np.int16))


def test_voice_model_download_is_checked(settings):
    def bad_download(url, path):
        path.write_bytes(b"not the model")

    with pytest.raises(RuntimeError, match="checksum"):
        speaker_id.ensure_model(settings.home, download=bad_download)
    assert not speaker_id.model_path(settings.home).exists()
    good = b"model"
    original = speaker_id.MODEL_SHA256
    speaker_id.MODEL_SHA256 = hashlib.sha256(good).hexdigest()
    try:
        assert speaker_id.ensure_model(settings.home, download=lambda u, p: p.write_bytes(good)).exists()
    finally:
        speaker_id.MODEL_SHA256 = original


def test_filterbank_shape():
    feats = speaker_id.fbank((np.random.default_rng(0).standard_normal(16_000) * 3000).astype(np.int16))
    assert feats.shape == (98, 80) and np.isfinite(feats).all()


# ---------------------------------------------------------------- the brain
def text(t):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text=t)]))])


class FakeGemini:
    def __init__(self, script):
        self.script, self.requests, self.models = list(script), [], self

    def generate_content(self, *, model, contents, config):
        self.requests.append(list(contents))
        return self.script.pop(0)


def brain(settings, store, script):
    fake = FakeGemini(script)
    return QuickBrain(settings, Gateway(settings, store), client=fake, models=["m"], active_window=lambda: ""), fake


def test_background_voices_get_no_answer(settings, store):
    qb, _ = brain(settings, store, [text("HEARD: (two people talking about football)\n[IGNORE]")])
    said, answer = qb.handle(audio=b"RIFF")
    assert answer == "" and store.recent_exchanges() == []


def test_correction_after_interrupting(settings, store):
    qb, fake = brain(settings, store, [
        text("HEARD: book a table for 4 at 8\nBooked a table for four at eight at Zooba."),
        text("HEARD: no, at 9\nChanged it to nine."),
    ])
    qb.handle(audio=b"RIFF")
    qb.handle(audio=b"RIFF2", interrupted="Booked a table for four…")
    sent = " ".join(p.text or "" for c in fake.requests[1] for p in c.parts)
    assert "interrupted your previous reply after hearing: «Booked a table for four…»" in sent
    assert "Zooba" not in sent  # history keeps only what they heard
    assert store.recent_exchanges()[0]["answer"].endswith("[interrupted]")


def test_assistant_handles_a_correction_right_away(settings, store):
    class Brain:
        def __init__(self):
            self.calls = []

        def handle(self, *, audio, interrupted=None):
            self.calls.append(interrupted)
            return ("open chrome", "Chrome is open. I also opened your usual tabs.") if interrupted is None \
                else ("no, just chrome", "Got it, only Chrome.")

    class Spk:
        def __init__(self):
            self.said = []

        def say(self, text, **kw):
            self.said.append(text)
            if len(self.said) == 1:
                return voice.Spoken(interrupted=True, played_s=1, heard_text="Chrome is open…", audio=b"RIFF2")
            return voice.Spoken()

    b, spk = Brain(), Spk()
    assert assistant.handle_one(settings, store, spk, lambda: b"RIFF", to="quick", new_session=True, quick=b)
    assert b.calls == [None, "Chrome is open…"] and spk.said[-1] == "Got it, only Chrome."


# ---------------------------------------------------------------- research stays in the background
def test_research_tools_are_named_for_what_they_do(settings, store):
    qb, _ = brain(settings, store, [])
    assert "show_google_results" in qb.tools and "google_search" not in qb.tools
    assert "background" in qb.tools["search_web"].__doc__
    assert "NEVER open a website or a Google results page to find something out" in qb.system_prompt()


def test_unreadable_page_is_read_by_google_instead(settings, store, monkeypatch):
    from jarvis import toolset

    monkeypatch.setattr("socket.getaddrinfo", lambda host, *_: [(0, 0, 0, "", ("93.184.216.34", 0))])

    class Http:
        def get(self, url):
            return SimpleNamespace(is_redirect=False, headers={"content-type": "text/html"}, status_code=200,
                                   content=b"<html><body><div id=root></div></body></html>", encoding="utf-8")

    asked = []
    gw = Gateway(settings, store, http_client=Http(),
                 ask_google=lambda q, model, urls=None: asked.append(urls) or {"answer": "It's a dashboard."})
    read = next(t for t in toolset.build(gw, include_assistant=True) if t.__name__ == "read_web_page")
    r = read("https://app.example.com")
    assert r["result"]["untrusted_web_answer"] == "It's a dashboard." and asked == [["https://app.example.com"]]
    assert gw.request("web_answer", {"question": "x", "urls": "file:///C:/secret"})["status"] == "error"


def test_close_tab_only_in_a_browser(settings, store):
    pressed = []
    gw = Gateway(settings, store, press_combo=pressed.append, foreground=lambda: ("chrome", "Docs - Google Chrome"))
    r = gw.request("close_browser_tab", {"count": 2})
    assert r["status"] == "ok" and pressed == [[0x11, 0x57], [0x11, 0x57]]
    gw2 = Gateway(settings, store, press_combo=pressed.append, foreground=lambda: ("WINWORD", "Thesis.docx"))
    assert gw2.request("close_browser_tab", {})["status"] == "error" and len(pressed) == 2


def test_filler_while_working_is_short_and_cached():
    assert all(len(p) <= 80 for lang in assistant.FILLERS.values() for p in lang)  # fits the phrase cache
    assert {"search_web", "read_web_page", "ask_claude"} <= assistant.SLOW_TOOLS


def test_turn_event_resets_fillers(settings, store):
    events = []
    qb = QuickBrain(settings, Gateway(settings, store), client=FakeGemini([text("HEARD: hi\nHi.")]), models=["m"],
                    active_window=lambda: "", on_event=lambda kind, **d: events.append(kind))
    qb.handle(audio=b"RIFF")
    assert events[0] == "turn"

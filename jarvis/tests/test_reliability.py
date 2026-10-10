"""Fixes from real use: echo cutting the voice off, tasks left unfinished, distance filtering,
false voice-lock rejections, and a report to see what happened."""

import time
from types import SimpleNamespace

import numpy as np

from jarvis import assistant, nearby, profile, quick, report, speaker_id, voice
from jarvis.gateway import Gateway


def speech(db_speech: float, db_noise: float = -70, seconds: float = 2.0) -> np.ndarray:
    """A recording: quiet room, then 'speech' at the given loudness (dBFS)."""
    rng = np.random.default_rng(1)
    amp = lambda db: 32768 * 10 ** (db / 20) * np.sqrt(2)  # noqa: E731
    quiet = rng.standard_normal(8000) * amp(db_noise) / np.sqrt(2)
    talk = np.sin(np.arange(int(16000 * seconds)) * 0.2) * amp(db_speech)
    return np.concatenate([quiet, talk, quiet]).astype(np.int16)


# ---------------------------------------------------------------- distance
def test_close_voices_pass_distant_ones_dont(store):
    gate = nearby.NearbyGate(store)
    assert gate.check(speech(-25))[0]  # clearly above the room: nearby
    assert not gate.check(speech(-62))[0]  # barely above the room: far away
    for _ in range(5):  # learns what requests at the desk sound like
        gate.check(speech(-24))
        gate.learn()
    assert gate.check(speech(-30))[0]  # a bit further / quieter: fine
    ok, info = gate.check(speech(-45))
    assert not ok and "accepts down to" in info["why"]  # across the room: ignored


def test_very_short_sounds_are_not_judged(store):
    assert nearby.NearbyGate(store).check(np.zeros(1000, dtype=np.int16))[0]


def test_old_profiles_turn_the_voice_lock_off_and_answer_people_nearby(settings):
    (settings.home / "profile.json").write_text('{"version": 2, "voice": "Ava", "voice_lock": true}')
    p = profile.Profile.load(settings.home)
    assert p.voice_lock is False and p.nearby_only is True
    assert profile.apply(p, "voice_lock", "on").voice_lock is True  # still available on request


def test_enrollment_fits_the_threshold_to_your_recordings(settings):
    class Emb:
        def __call__(self, samples):
            e = np.array([1.0, 0.0]) + np.array([0.0, float(np.median(samples)) / 3000])
            return e / np.linalg.norm(e)

    vp = speaker_id.VoicePrint(settings.home, embedder=Emb())
    result = vp.enroll([np.full(16000, v, dtype=np.int16) for v in (400, 6000, 12000)])  # a varied voice
    assert result["threshold"] < speaker_id.DEFAULT_THRESHOLD  # more forgiving than the fixed default
    assert speaker_id.VoicePrint(settings.home, embedder=Emb()).threshold == result["threshold"]  # remembered


# ---------------------------------------------------------------- the voice never silently stops
def test_noise_mistaken_for_an_interruption_resumes_the_reply(settings, store):
    class Brain:
        calls = 0

        def handle(self, *, audio, interrupted=None):
            Brain.calls += 1
            return "tell me about mars", "Mars is the fourth planet. It has two small moons."

    class Spk:
        said = []

        def say(self, text, **kw):
            Spk.said.append(text)
            if len(Spk.said) == 1:  # "interrupted" by a click: no speech in it
                return voice.Spoken(True, 1.2, "Mars is the fourth planet.…",
                                    audio=voice.to_wav(np.zeros(16000, dtype=np.int16).tobytes()))
            return voice.Spoken()

    assert assistant.handle_one(settings, store, Spk(), lambda: b"RIFF", to="quick", new_session=True,
                                quick=Brain())
    assert Spk.said == ["Mars is the fourth planet. It has two small moons.", "It has two small moons."]
    assert Brain.calls == 1  # the noise was never sent to Gemini as a "correction"
    kinds = [e["kind"] for e in store.events(limit=50) if e["stage"] == "voice"]
    assert "false_interrupt" in kinds


def test_default_interrupt_mode_depends_on_headphones(monkeypatch):
    for name, expect in (("Speakers (Realtek(R) Audio)", False), ("Headphones (WH-1000XM4)", True),
                         ("Headset Earphone (AirPods)", True)):
        monkeypatch.setattr(voice, "_sounddevice", lambda n=name: SimpleNamespace(
            query_devices=lambda kind: {"name": n}))
        assert voice.headphones_in_use() is expect


# ---------------------------------------------------------------- finishing tasks
def test_prompt_is_flexible_and_finishes_tasks():
    text = quick.build_system_prompt(profile.Profile(), [], {}, [], "now")
    assert "Finish the WHOLE request" in text and "try another" in text
    assert "تمام" in text and "don't ask again" in text
    assert "Anyone near the laptop may talk to you" in text
    assert 'Don\'t ask "Shall I?" for' in text  # clear requests are just done


def test_everyday_words_dont_become_permanent_rules():
    assert not quick.looks_like_rule("always open chrome in incognito")  # a request, not a rule
    assert not quick.looks_like_rule("افتح الملف ده دايما")
    assert quick.looks_like_rule("from now on open chrome in incognito")
    assert quick.looks_like_rule("بعد كده افتح كروم مش ايدج")


def test_too_many_steps_says_what_was_done(settings, store):
    from google.genai import types

    def call():
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(name="system_info", args={}))]))])

    fake = SimpleNamespace(requests=[])
    fake.models = SimpleNamespace(generate_content=lambda **kw: call())
    qb = quick.QuickBrain(settings, Gateway(settings, store), client=fake, models=["m"], max_rounds=3,
                          active_window=lambda: "")
    _, answer = qb.handle(audio=b"RIFF")
    assert "system info" in answer and "continue" in answer


# ---------------------------------------------------------------- report
def test_report_explains_what_happened(settings, store):
    store.add_exchange("open the report", "I couldn't find it.")
    store.log("hands", "failed", "no file named report", action="open_path")
    store.log("voice", "voice_failed", "edge: ConnectionError")
    store.log("voice", "ignored_far", "speech -48 dB")
    store.remember("Use Chrome, not Edge", "instructions")
    text = report.build(store, settings.home, hours=1, now=time.time() + 1)
    assert "open the report" in text and "no file named report" in text
    assert "Voice problems: 1" in text and "distant voices 1" in text and "Use Chrome, not Edge" in text

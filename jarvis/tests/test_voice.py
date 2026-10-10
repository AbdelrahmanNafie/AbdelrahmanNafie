from types import SimpleNamespace

import pytest

from jarvis import assistant, voice
from jarvis.bridge import BrainError
from jarvis.ears import Heard


def _run(detector, levels):
    for i, lv in enumerate(levels):
        if detector.feed(lv, dt=0.1):
            return i
    return None


def test_default_waits_two_seconds_of_silence():
    d = voice.SilenceDetector(noise_floor=200)
    levels = [3000] * 10 + [100] * 15 + [3000] * 10 + [100] * 30  # 1.5 s thinking pause
    assert _run(d, levels) == 10 + 15 + 10 + 19


def test_quiet_speaker_is_still_heard():
    d = voice.SilenceDetector(noise_floor=150)
    assert d.threshold == 350
    d.feed(400)
    assert d.heard_speech


def test_recording_stops_after_silence_following_speech():
    d = voice.SilenceDetector(noise_floor=200, silence_s=1.0)
    stop = _run(d, [100] * 5 + [3000] * 20 + [100] * 30)
    assert d.heard_speech and stop == 5 + 20 + 9  # ~1 s of quiet after 2 s of speech


def test_short_pauses_do_not_cut_you_off():
    d = voice.SilenceDetector(noise_floor=200, silence_s=1.0)
    levels = [3000] * 10 + [100] * 5 + [3000] * 10 + [100] * 20  # 0.5 s pause mid-sentence
    assert _run(d, levels) == 10 + 5 + 10 + 9


def test_gives_up_when_nobody_speaks():
    d = voice.SilenceDetector(noise_floor=200, no_speech_s=2.0)
    assert _run(d, [100] * 50) == 19 and not d.heard_speech


def test_threshold_adapts_to_noisy_room():
    assert voice.SilenceDetector(noise_floor=1000).threshold == 2000
    assert voice.SilenceDetector(noise_floor=10).threshold == 350


def test_windows_voice_passes_text_on_stdin_not_command_line(monkeypatch):
    calls = []
    monkeypatch.setattr(voice.sys, "platform", "win32")
    spk = voice.Speaker("windows", runner=lambda cmd, **kw: calls.append((cmd, kw)))
    spk.say('Done"; Remove-Item C:\\ -Recurse; "')
    cmd, kw = calls[0]
    assert "Remove-Item" not in " ".join(cmd)
    assert "Remove-Item" in kw["input"]


def test_gemini_voice_falls_back_to_windows_voice(monkeypatch):
    calls = []
    monkeypatch.setattr(voice.sys, "platform", "win32")

    class Broken:
        class models:
            @staticmethod
            def generate_content(**_):
                raise RuntimeError("503 busy")

    spk = voice.Speaker("gemini", client=Broken(), runner=lambda cmd, **kw: calls.append(kw["input"]))
    spk.say("hello")
    assert calls == ["hello"]


def test_gemini_pcm_is_wrapped_as_wav():
    pcm = b"\x00\x01" * 100
    part = SimpleNamespace(inline_data=SimpleNamespace(data=pcm))
    resp = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: resp))
    wav = voice.Speaker("gemini", client=client)._gemini_tts("hi")
    assert wav[:4] == b"RIFF" and wav.endswith(pcm)


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


@pytest.fixture
def fake_ears(monkeypatch):
    heard = Heard(original="افتح النوت باد", language="egyptian_arabic", english="Open Notepad")
    monkeypatch.setattr(assistant, "hear", lambda *a, **k: heard)
    return heard


def test_assistant_speaks_claude_reply(settings, store, fake_ears, monkeypatch):
    monkeypatch.setattr(assistant, "ask_brain", lambda *a, **k: "Notepad is open.")
    spk = FakeSpeaker()
    assistant.handle_one(settings, store, spk, lambda: b"RIFF", to="claude", new_session=True)
    assert spk.said == ["On it.", "Notepad is open."]


def test_assistant_test_mode_repeats_what_it_heard(settings, store, fake_ears):
    spk = FakeSpeaker()
    assistant.handle_one(settings, store, spk, lambda: b"RIFF", to="print", new_session=True)
    assert spk.said == ["I heard: Open Notepad"]


def test_assistant_survives_claude_being_unavailable(settings, store, fake_ears, monkeypatch):
    def boom(*a, **k):
        raise BrainError("weekly limit")
    monkeypatch.setattr(assistant, "ask_brain", boom)
    spk = FakeSpeaker()
    assistant.handle_one(settings, store, spk, lambda: b"RIFF", to="claude", new_session=True)
    assert "not available" in spk.said[-1]


def test_assistant_handles_silence(settings, store):
    spk = FakeSpeaker()
    assistant.handle_one(settings, store, spk, lambda: None, to="claude", new_session=True)
    assert spk.said == ["I didn't hear anything."]


def _chunks(n, value=1000):
    import numpy as np
    return (np.full(1280, value, dtype=np.int16) for _ in range(n))


def test_wake_loop_fires_on_score():
    scores = iter([0.1, 0.2, 0.6])
    assert voice.wake_loop(_chunks(10), lambda c: next(scores), 0.5) == 0.6


def test_wake_loop_stops_on_enter_and_reports_progress():
    ticks = []
    result = voice.wake_loop(_chunks(100), lambda c: 0.1, 0.5, should_stop=lambda: len(ticks) >= 2,
                             on_tick=lambda lv, best, th: ticks.append((round(lv), best)))
    assert result is None and ticks == [(1000, 0.1), (1000, 0.1)]


def test_meter_warns_when_mic_is_silent(capsys):
    voice.meter(10, 0.0, 0.4)
    assert "mic silent" in capsys.readouterr().out


def test_meter_line_erases_previous_longer_line(capsys):
    voice.meter(10, 0.0, 0.4)      # long line with the "mic silent?" hint
    voice.meter(6000, 0.2, 0.4)    # loud: no hint
    last = capsys.readouterr().out.split("\r")[-1]
    assert "mic silent" not in last and len(last) >= voice._METER_WIDTH

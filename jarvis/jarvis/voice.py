"""Microphone, wake word and speaker.

- record_until_silence: starts when you speak, stops ~2 s after you stop.
- WakeWord: local "Hey Jarvis" detector (openWakeWord); no audio leaves the laptop.
- Speaker: says the reply out loud (Windows voice by default, Gemini voice optional).

Audio libraries are imported lazily so the rest of Jarvis works without them.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import wave
from dataclasses import dataclass

RATE = 16_000
CHUNK_S = 0.08  # 80 ms = 1280 samples, the frame size openWakeWord expects
CHUNK = int(RATE * CHUNK_S)
_EPS = 1e-6  # float sums of chunk durations drift slightly


def _sounddevice():
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise RuntimeError("Microphone support needs: pip install -e \".[mic]\"") from exc
    return sd


def level(chunk) -> float:
    """Loudness (RMS) of an int16 numpy chunk."""
    import numpy as np

    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) if len(chunk) else 0.0


@dataclass
class SilenceDetector:
    """Decides when an utterance is over, from a stream of loudness values (one per chunk)."""

    noise_floor: float = 300.0
    silence_s: float = 2.0  # this much quiet after speech ends the recording (natural pauses are shorter)
    max_s: float = 30.0
    no_speech_s: float = 8.0  # give up if nothing is said at all
    _speech_started: bool = False
    _quiet: float = 0.0
    _elapsed: float = 0.0

    @property
    def threshold(self) -> float:
        return max(self.noise_floor * 2.0, 350.0)

    @property
    def heard_speech(self) -> bool:
        return self._speech_started

    def feed(self, loudness: float, dt: float = CHUNK_S) -> bool:
        """Returns True when recording should stop."""
        self._elapsed += dt
        if loudness >= self.threshold:
            self._speech_started = True
            self._quiet = 0.0
        elif self._speech_started:
            self._quiet += dt
        if self._elapsed >= self.max_s - _EPS:
            return True
        if not self._speech_started:
            return self._elapsed >= self.no_speech_s - _EPS
        return self._quiet >= self.silence_s - _EPS


def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def record_until_silence(*, max_s: float = 30.0, on_start=None) -> bytes | None:
    """Record one utterance. Returns WAV bytes, or None if nobody spoke."""
    import numpy as np

    sd = _sounddevice()
    frames = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
        # Measure the room's background noise for ~0.3 s so the threshold adapts.
        calib = [stream.read(CHUNK)[0][:, 0] for _ in range(4)]
        detector = SilenceDetector(noise_floor=float(np.median([level(c) for c in calib])), max_s=max_s)
        if on_start:
            on_start()  # e.g. the "speak now" beep: played only once the mic is really ready
            for _ in range(3):  # drop ~0.24 s so the beep itself isn't taken for your voice
                stream.read(CHUNK)
        while True:
            chunk = stream.read(CHUNK)[0][:, 0].copy()
            frames.append(chunk)
            if detector.feed(level(chunk)):
                break
    seconds = len(frames) * CHUNK_S
    if not detector.heard_speech:
        return None
    note = " (hit the time limit: background noise may be too loud)" if seconds >= max_s - 0.1 else ""
    print(f"⏹  Recorded {seconds:.1f}s{note}", flush=True)
    return to_wav(np.concatenate(frames).tobytes())


class WakeWord:
    """Blocks until the wake word is heard. Runs fully offline."""

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.4):
        try:
            from openwakeword import utils
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("Wake word needs: pip install -e \".[wake]\"") from exc
        utils.download_models(model_names=[model])  # no-op once downloaded
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.name = model
        self.threshold = threshold

    def wait(self, *, should_stop=None, show_meter: bool = True) -> float | None:
        """Block until the wake word (returns its score) or should_stop() is true (returns None)."""
        sd = _sounddevice()
        self.model.reset()
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
            chunks = (stream.read(CHUNK)[0][:, 0] for _ in iter(int, 1))
            return wake_loop(chunks, lambda c: self.model.predict(c)[self.name], self.threshold,
                             should_stop=should_stop, on_tick=meter if show_meter else None)


def wake_loop(chunks, score_of, threshold: float, *, should_stop=None, on_tick=None,
              tick_every: int = 6) -> float | None:
    """Core of the wake-word wait, separated from the microphone so it can be tested.

    Every `tick_every` chunks (~0.5 s) reports (loudest level, best score) to on_tick,
    so the user can see the mic is alive and how close the wake word came.
    """
    loudest = best = 0.0
    for i, chunk in enumerate(chunks, 1):
        score = float(score_of(chunk))
        if score >= threshold:
            if on_tick:
                print(flush=True)  # end the meter line
            return score
        loudest, best = max(loudest, level(chunk)), max(best, score)
        if i % tick_every == 0:
            if on_tick:
                on_tick(loudest, best, threshold)
            loudest = best = 0.0
            if should_stop and should_stop():
                if on_tick:
                    print(flush=True)
                return None
    return None


_METER_WIDTH = 100


def meter(loudest: float, best: float, threshold: float) -> None:
    """One self-updating line: mic volume bar + wake word score."""
    bars = min(20, int(loudest / 300))
    hint = "  (mic silent? check Windows sound input)" if loudest < 60 else ""
    line = f"   mic {'█' * bars}{'·' * (20 - bars)}  wake score {best:.2f}/{threshold:.2f}  — or press Enter{hint}"
    # Pad to a fixed width so a shorter line fully overwrites the previous one.
    print("\r" + line.ljust(_METER_WIDTH), end="", flush=True)


def yes_no_pressed() -> bool | None:
    """Non-blocking: True if Y was pressed, False if N, else None (Windows console only)."""
    if sys.platform != "win32":
        return None
    import msvcrt

    answer = None
    while msvcrt.kbhit():
        key = msvcrt.getwch().lower()
        if key in ("y", "n"):
            answer = key == "y"
    return answer


def enter_pressed() -> bool:
    """Non-blocking check for the Enter key (Windows console); always False elsewhere."""
    if sys.platform != "win32":
        return False
    import msvcrt

    pressed = False
    while msvcrt.kbhit():
        if msvcrt.getwch() in ("\r", "\n"):
            pressed = True
    return pressed


def chime(kind: str = "start") -> None:
    """High beep = speak now; low beep = stopped listening."""
    if sys.platform == "win32":
        import winsound

        winsound.Beep(*((1046, 110) if kind == "start" else (523, 140)))
    else:
        print("\a", end="", flush=True)


# ---------------------------------------------------------------- speaking
_WINDOWS_SAY = (
    "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
    "Add-Type -AssemblyName System.Speech;"
    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.Rate=1;"
    "$s.Speak([Console]::In.ReadToEnd())"
)


class Speaker:
    """voice='windows' (free, instant), 'gemini' (natural, uses Gemini credits) or 'off'."""

    def __init__(self, voice: str = "windows", *, gemini_model: str = "gemini-3.8-flash-tts",
                 gemini_voice: str = "Kore", client=None, runner=subprocess.run):
        self.voice = voice
        self.gemini_model = gemini_model
        self.gemini_voice = gemini_voice
        self.client = client
        self.runner = runner

    def say(self, text: str) -> None:
        text = text.strip()
        if not text or self.voice == "off":
            return
        if self.voice == "gemini":
            try:
                return self._play_wav(self._gemini_tts(text))
            except Exception as exc:  # noqa: BLE001 — never lose the answer because of the voice
                print(f"(Gemini voice failed: {exc}; using the Windows voice)")
        self._windows(text)

    def _windows(self, text: str) -> None:
        if sys.platform != "win32":
            print(f"🔊 {text}")
            return
        sapi = self._sapi()
        if sapi is not None:
            sapi.Speak(text)  # direct call: no 1-2 s PowerShell start-up
            return
        # Fallback. Text goes in on stdin, never into the command line, so nothing in it can run as code.
        self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SAY],
                    input=text, text=True, encoding="utf-8", capture_output=True)

    _sapi_voice = None

    def _sapi(self):
        if self.runner is not subprocess.run:  # tests inject a runner: use the PowerShell path
            return None
        if Speaker._sapi_voice is None:
            try:
                import win32com.client

                Speaker._sapi_voice = win32com.client.Dispatch("SAPI.SpVoice")
            except Exception:  # noqa: BLE001 — pywin32 missing: fall back to PowerShell
                Speaker._sapi_voice = False
        return Speaker._sapi_voice or None

    def _gemini_tts(self, text: str) -> bytes:
        from google import genai
        from google.genai import types

        client = self.client or genai.Client()
        response = client.models.generate_content(
            model=self.gemini_model,
            contents=f"Say in a calm, friendly assistant voice: {text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.gemini_voice))),
            ),
        )
        data = response.candidates[0].content.parts[0].inline_data.data
        # Docs describe raw 24 kHz 16-bit mono PCM; accept a ready WAV too.
        return data if data[:4] == b"RIFF" else to_wav(data, rate=24_000)

    @staticmethod
    def _play_wav(wav_bytes: bytes) -> None:
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
            return
        import numpy as np

        sd = _sounddevice()
        with wave.open(io.BytesIO(wav_bytes)) as wav:
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
            sd.play(pcm, wav.getframerate(), blocking=True)


def default_voice() -> str:
    return os.environ.get("JARVIS_VOICE", "windows")

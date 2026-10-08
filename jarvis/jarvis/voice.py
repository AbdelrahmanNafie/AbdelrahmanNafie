"""Microphone, wake word and speaker.

- record_until_silence: starts when you speak, stops ~1.2 s after you stop.
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
    silence_s: float = 1.2  # this much quiet after speech ends the recording
    max_s: float = 30.0
    no_speech_s: float = 6.0  # give up if nothing is said at all
    _speech_started: bool = False
    _quiet: float = 0.0
    _elapsed: float = 0.0

    @property
    def threshold(self) -> float:
        return max(self.noise_floor * 2.5, 500.0)

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
            on_start()
        while True:
            chunk = stream.read(CHUNK)[0][:, 0].copy()
            frames.append(chunk)
            if detector.feed(level(chunk)):
                break
    if not detector.heard_speech:
        return None
    return to_wav(np.concatenate(frames).tobytes())


class WakeWord:
    """Blocks until the wake word is heard. Runs fully offline."""

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5):
        try:
            from openwakeword import utils
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("Wake word needs: pip install -e \".[wake]\"") from exc
        utils.download_models(model_names=[model])  # no-op once downloaded
        self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.name = model
        self.threshold = threshold

    def wait(self) -> float:
        sd = _sounddevice()
        self.model.reset()
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
            while True:
                chunk = stream.read(CHUNK)[0][:, 0]
                score = self.model.predict(chunk)[self.name]
                if score >= self.threshold:
                    return float(score)


def chime() -> None:
    """Short 'I'm listening' beep."""
    if sys.platform == "win32":
        import winsound

        winsound.Beep(880, 120)
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
        # Text goes in on stdin, never into the command line, so nothing in it can run as code.
        self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SAY],
                    input=text, text=True, encoding="utf-8", capture_output=True)

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

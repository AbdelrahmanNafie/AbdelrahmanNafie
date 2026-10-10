"""Microphone, wake word and speaker.

- record_until_silence: starts when you speak, stops ~2 s after you stop.
- WakeWord: local "Hey Jarvis" detector (openWakeWord); no audio leaves the laptop.
- Speaker: says the reply out loud (natural Gemini voice, Windows voice as fallback).

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
    silence_s: float = 1.6  # this much quiet after speech ends the recording (natural pauses are shorter)
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


def record_until_silence(*, max_s: float = 30.0, no_speech_s: float = 8.0, on_start=None,
                         on_level=None) -> bytes | None:
    """Record one utterance. Returns WAV bytes, or None if nobody spoke."""
    import numpy as np

    sd = _sounddevice()
    frames = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
        # Measure the room's background noise for ~0.3 s so the threshold adapts.
        calib = [stream.read(CHUNK)[0][:, 0] for _ in range(2)]  # ~0.16 s
        detector = SilenceDetector(noise_floor=float(np.median([level(c) for c in calib])), max_s=max_s,
                                   no_speech_s=no_speech_s)
        if on_start:
            on_start()  # e.g. the "speak now" beep: played only once the mic is really ready
            for _ in range(3):  # drop ~0.24 s so the beep itself isn't taken for your voice
                stream.read(CHUNK)
        while True:
            chunk = stream.read(CHUNK)[0][:, 0].copy()
            frames.append(chunk)
            loud = level(chunk)
            if on_level:
                on_level(min(1.0, loud / 4000))  # 0..1 for the orb
            if detector.feed(loud):
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
    quiet_ticks = 0
    for i, chunk in enumerate(chunks, 1):
        score = float(score_of(chunk))
        if score >= threshold:
            if on_tick:
                print(flush=True)  # end the meter line
            return score
        loudest, best = max(loudest, level(chunk)), max(best, score)
        if i % tick_every == 0:
            quiet_ticks = quiet_ticks + 1 if loudest < 60 else 0
            if on_tick:
                on_tick(loudest, best, threshold, quiet_ticks)
            loudest = best = 0.0
            if should_stop and should_stop():
                if on_tick:
                    print(flush=True)
                return None
    return None


_METER_WIDTH = 100


def meter(loudest: float, best: float, threshold: float, quiet_ticks: int = 99) -> None:
    """One self-updating line: mic volume bar + wake word score."""
    bars = min(20, int(loudest / 300))
    hint = "  (mic silent? check Windows sound input)" if quiet_ticks >= 10 else ""  # ~5 s of silence
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
    "try{$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)}catch{};"
    "$s.Rate=1;"
    "$s.Speak([Console]::In.ReadToEnd())"
)

# How the Gemini voice should sound. Gemini TTS follows plain-language style directions.
_STYLE = ("Read this aloud as a warm, friendly young woman talking naturally to a friend: relaxed pace, "
          "real intonation, light smile in the voice. Read only the text after the colon")


def split_for_speech(text: str, first_max: int = 140) -> list[str]:
    """First sentence alone (so the voice starts sooner), the rest in one piece.

    Two TTS calls at most per reply: quick start without burning extra Gemini quota.
    """
    import re

    text = " ".join(text.split())
    if len(text) <= first_max:
        return [text] if text else []
    m = re.search(r"(?<=[.!?؟。])\s", text[: first_max + 1])
    if not m:
        return [text]
    return [text[: m.start()].strip(), text[m.end():].strip()]


class Speaker:
    """Says replies out loud.

    voice='gemini'  natural Gemini voice (default; female 'Aoede'), falls back to Windows on errors
    voice='windows' built-in Windows voice (female if one is installed); free and instant
    voice='off'     silent
    """

    QUOTA_COOLDOWN_S = 600  # after a 429, use the Windows voice for a while instead of failing every reply

    def __init__(self, voice: str = "gemini", *, gemini_model: str | None = None,
                 gemini_voice: str = "Aoede", client=None, runner=subprocess.run, on_level=None):
        self.voice = voice
        self.gemini_model = gemini_model or os.environ.get("JARVIS_TTS_MODEL", "gemini-3.8-flash-tts")
        self.gemini_voice = gemini_voice
        self.client = client
        self.runner = runner
        self.on_level = on_level  # 0..1 loudness while speaking, for the orb
        self._gemini_off_until = 0.0

    def set_voice(self, name: str) -> None:
        """'windows' or any Gemini voice name (from the profile)."""
        if self.voice == "off":
            return
        if name == "windows":
            self.voice = "windows"
        else:
            self.voice, self.gemini_voice = "gemini", name

    def say(self, text: str, *, from_other_thread: bool = False) -> None:
        import time

        text = text.strip()
        if not text or self.voice == "off":
            return
        if self.voice == "gemini" and time.monotonic() >= self._gemini_off_until:
            try:
                return self._say_gemini(text)
            except Exception as exc:  # noqa: BLE001 — never lose the answer because of the voice
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    self._gemini_off_until = time.monotonic() + self.QUOTA_COOLDOWN_S
                    print("(Gemini voice is over its quota; using the Windows voice for 10 minutes)")
                else:
                    print(f"(Gemini voice failed: {str(exc)[:160]}; using the Windows voice)")
        if from_other_thread:  # e.g. a reminder thread: avoid the main thread's COM voice
            return self._powershell_say(text)
        self._windows(text)

    # ------------------------------------------------------------ gemini
    def _say_gemini(self, text: str) -> None:
        """Synthesize the next piece while the current one plays."""
        from concurrent.futures import ThreadPoolExecutor

        parts = split_for_speech(text)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._gemini_tts, parts[0])
            for i in range(len(parts)):
                wav = pending.result()
                if i + 1 < len(parts):
                    pending = pool.submit(self._gemini_tts, parts[i + 1])
                self._play_wav(wav)

    def _gemini_tts(self, text: str) -> bytes:
        from google.genai import types

        if self.client is None:
            from .ears import make_client

            self.client = make_client()
        response = self.client.models.generate_content(
            model=self.gemini_model,
            contents=f"{_STYLE}: {text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.gemini_voice))),
            ),
        )
        data = response.candidates[0].content.parts[0].inline_data.data
        # Docs describe raw 24 kHz 16-bit mono PCM; accept a ready WAV too.
        return data if data[:4] == b"RIFF" else to_wav(data, rate=24_000)

    def _play_wav(self, wav_bytes: bytes) -> None:
        import numpy as np

        with wave.open(io.BytesIO(wav_bytes)) as wav:
            rate = wav.getframerate()
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        try:
            sd = _sounddevice()
        except RuntimeError:
            sd = None
        if sd is None:
            if sys.platform == "win32":
                import winsound

                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
            return
        block = rate // 20  # 50 ms blocks: write() paces playback, and we report each block's loudness
        with sd.OutputStream(samplerate=rate, channels=1, dtype="int16") as out:
            for i in range(0, len(pcm), block):
                chunk = pcm[i:i + block]
                if self.on_level:
                    self.on_level(min(1.0, level(chunk) / 6000))
                out.write(chunk.reshape(-1, 1))

    # ----------------------------------------------------------- windows
    def _windows(self, text: str) -> None:
        if sys.platform != "win32":
            print(f"🔊 {text}")
            return
        sapi = self._sapi()
        if sapi is not None:
            sapi.Speak(text)  # direct call: no 1-2 s PowerShell start-up
            return
        self._powershell_say(text)

    def _powershell_say(self, text: str) -> None:
        if sys.platform != "win32" and self.runner is subprocess.run:
            print(f"🔊 {text}")
            return
        # Text goes in on stdin, never into the command line, so nothing in it can run as code.
        self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SAY],
                    input=text, text=True, encoding="utf-8", capture_output=True)

    _sapi_voice = None

    def _sapi(self):
        if self.runner is not subprocess.run:  # tests inject a runner: use the PowerShell path
            return None
        if Speaker._sapi_voice is None:
            try:
                import win32com.client

                sapi = win32com.client.Dispatch("SAPI.SpVoice")
                female = sapi.GetVoices("Gender=Female")
                if female.Count:
                    sapi.Voice = female.Item(0)  # e.g. Microsoft Zira
                Speaker._sapi_voice = sapi
            except Exception:  # noqa: BLE001 — pywin32 missing: fall back to PowerShell
                Speaker._sapi_voice = False
        return Speaker._sapi_voice or None


def default_voice() -> str:
    """JARVIS_VOICE overrides; otherwise the natural Gemini voice (falls back to Windows on its own)."""
    return os.environ.get("JARVIS_VOICE", "gemini")

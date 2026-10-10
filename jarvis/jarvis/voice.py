"""Microphone, wake word and speaker.

- record_until_silence: starts when you speak, stops ~2 s after you stop.
- WakeWord: local "Hey Jarvis" detector (openWakeWord); no audio leaves the laptop.
- Speaker: says the reply out loud (natural Gemini voice, Windows voice as fallback).

Audio libraries are imported lazily so the rest of Jarvis works without them.
"""

from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
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

    def feed(self, loudness: float, dt: float = CHUNK_S, speech: bool | None = None) -> bool:
        """Returns True when recording should stop.

        speech: the voice detector's verdict for this chunk (None = no detector). Loud noise that
        isn't speech (fan, traffic, music without vocals) then neither starts nor extends a recording.
        """
        self._elapsed += dt
        if loudness >= self.threshold and speech is not False:
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


class SpeechVAD:
    """Silero voice-activity detector (bundled with openWakeWord): speech vs. other sounds.

    Optional: without openWakeWord it says None and recording falls back to loudness only.
    """

    _shared: "SpeechVAD | None" = None

    def __init__(self):
        self.model = None
        try:
            from openwakeword.vad import VAD

            self.model = VAD()
        except Exception:  # noqa: BLE001 — not installed / model not downloaded yet
            self.model = None

    @classmethod
    def shared(cls) -> "SpeechVAD":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def reset(self) -> None:
        if self.model is not None:
            self.model.reset_states()

    def is_speech(self, chunk, threshold: float = 0.5) -> bool | None:
        if self.model is None:
            return None
        try:
            return float(self.model.predict(chunk, frame_size=640)) >= threshold
        except Exception:  # noqa: BLE001
            return None


def record_until_silence(*, max_s: float = 30.0, no_speech_s: float = 8.0, on_start=None,
                         on_level=None) -> bytes | None:
    """Record one utterance. Returns WAV bytes, or None if nobody spoke."""
    import numpy as np

    sd = _sounddevice()
    vad = SpeechVAD.shared()
    vad.reset()
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
            if detector.feed(loud, speech=vad.is_speech(chunk)):  # fed every chunk: the detector keeps state
                break
    seconds = len(frames) * CHUNK_S
    if not detector.heard_speech:
        return None
    note = " (hit the time limit: background noise may be too loud)" if seconds >= max_s - 0.1 else ""
    print(f"⏹  Recorded {seconds:.1f}s{note}", flush=True)
    return to_wav(np.concatenate(frames).tobytes())


class WakeWord:
    """Blocks until the wake word is heard. Runs fully offline."""

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.4, vad_threshold: float = 0.5):
        try:
            from openwakeword import utils
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("Wake word needs: pip install -e \".[wake]\"") from exc
        utils.download_models(model_names=[model])  # no-op once downloaded
        try:  # the voice detector gate: noise alone can't trigger the wake word
            self.model = Model(wakeword_models=[model], inference_framework="onnx", vad_threshold=vad_threshold)
        except Exception:  # noqa: BLE001 — older openWakeWord / VAD model missing
            self.model = Model(wakeword_models=[model], inference_framework="onnx")
        self.name = model
        self.threshold = threshold

    def score(self, chunk) -> float:
        """Wake word score for one 80 ms chunk (used to interrupt Jarvis while it speaks)."""
        return float(self.model.predict(chunk)[self.name])

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


def escape_pressed() -> bool:
    """Non-blocking check for Esc (Windows console): stops Jarvis talking."""
    if sys.platform != "win32":
        return False
    import msvcrt

    pressed = False
    while msvcrt.kbhit():
        if msvcrt.getwch() == "\x1b":
            pressed = True
    return pressed


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

# Only the reply itself goes to the speech model. (A style instruction in front of it can get
# read out loud as part of every answer.) Optional short style, e.g. JARVIS_TTS_STYLE="Say warmly".
_STYLE = os.environ.get("JARVIS_TTS_STYLE", "").strip()

GEMINI_VOICES = {
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
}
# Microsoft neural voices (free, no quota, no key; through the edge-tts package). Female picks.
EDGE_VOICES = {
    "ava": "en-US-AvaMultilingualNeural",  # natural, also reads Arabic
    "emma": "en-US-EmmaMultilingualNeural",
    "jenny": "en-US-JennyNeural",
    "aria": "en-US-AriaNeural",
    "sonia": "en-GB-SoniaNeural",
    "salma": "ar-EG-SalmaNeural",  # Egyptian Arabic
    "zariyah": "ar-SA-ZariyahNeural",
}
DEFAULT_VOICE = "Ava"


def engine_for(name: str) -> tuple[str, str]:
    """Voice name → (engine, engine-specific voice): windows | gemini | edge."""
    import re

    n = (name or "").strip()
    low = n.lower()
    if low in ("windows", ""):
        return "windows", ""
    for g in GEMINI_VOICES:
        if g.lower() == low:
            return "gemini", g
    if low in EDGE_VOICES:
        return "edge", EDGE_VOICES[low]
    if re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}-\w+Neural", n):
        return "edge", n
    return "gemini", n[:1].upper() + n[1:]


def split_for_speech(text: str, first_max: int = 140) -> list[str]:
    """Sentences, with very short ones joined to the next (each piece is spoken in the same voice)."""
    import re

    text = " ".join(text.split())
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?؟。])\s+", text) if p.strip()]
    merged: list[str] = []
    for part in parts:
        if merged and (len(merged[-1]) < 25 or len(part) < 12) and len(merged[-1]) + len(part) < first_max * 2:
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged


@dataclass
class Spoken:
    """What happened while speaking."""

    interrupted: bool = False
    played_s: float = 0.0
    heard_text: str = ""  # roughly what the user heard before interrupting
    audio: bytes | None = None  # what they said when they interrupted (filled in by the assistant)


def _heard_part(text: str, played_s: float, chars_per_s: float = 14.0) -> str:
    cut = int(played_s * chars_per_s)
    if cut >= len(text):
        return text
    space = text.rfind(" ", 0, cut)
    return text[: space if space > 0 else cut] + "…"


class _Stopped(Exception):
    pass


class _Partial(Exception):
    """The voice failed partway: the rest of the reply still has to be said."""

    def __init__(self, remaining: str, played_s: float, cause: Exception):
        super().__init__(str(cause))
        self.remaining, self.played_s, self.cause = remaining, played_s, cause


class _QuotaError(RuntimeError):
    pass


def _edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        return False
    return True


class Speaker:
    """Says replies out loud, always in one voice per reply.

    voice (engine): 'edge'    Microsoft neural voice, e.g. Ava (free, no daily limit) — recommended
                    'gemini'  Gemini voice, e.g. Aoede (very natural, but only ~10-100 replies a day)
                    'windows' built-in Windows voice (offline, robotic)
                    'off'     silent
    If an engine fails, the next one says what's left (a sentence that failed is retried once
    first). Nothing is ever skipped silently: you always hear the whole reply.
    """

    QUOTA_COOLDOWN_S = 3600

    def __init__(self, voice: str = "gemini", *, gemini_model: str | None = None,
                 gemini_voice: str = "Aoede", edge_voice: str = EDGE_VOICES["ava"], client=None,
                 runner=subprocess.run, on_level=None, cache_dir=None, log=None):
        import time

        self.voice = voice
        models = [gemini_model or os.environ.get("JARVIS_TTS_MODEL", "gemini-3.8-flash-tts")]
        models += [m.strip() for m in os.environ.get("JARVIS_TTS_FALLBACKS", "gemini-3.8-flash-lite-tts").split(",")
                   if m.strip() and m.strip() not in models]
        self.gemini_models = models
        self.gemini_model = models[0]
        self.gemini_voice = gemini_voice
        self.edge_voice = edge_voice
        self.client = client
        self.runner = runner
        self.on_level = on_level  # 0..1 loudness while speaking, for the orb and echo filtering
        self.cache_dir = cache_dir  # short phrases are synthesized once, then replayed for free
        self._cooled: dict[str, float] = {}  # gemini model → usable again at (monotonic)
        self._gemini_off_until = 0.0
        self._can_stream = True
        self._notified: set[str] = set()
        self.first_audio_s: float | None = None
        self.engine_used = ""
        self._clock = time.monotonic
        self.log = log or (lambda kind, summary: None)  # → audit log, for `python -m jarvis.report`

    # ------------------------------------------------------------ choosing
    def set_voice(self, name: str) -> None:
        """A profile voice name: 'windows', a Gemini voice (Aoede, Kore…) or an Edge voice (Ava, Salma…)."""
        if self.voice == "off":
            return
        engine, resolved = engine_for(name)
        if engine == "edge" and not _edge_available():
            self._notice("edge-missing", "(The Ava/Edge voices need: pip install -e \".[voice]\" — using Gemini's voice)")
            engine, resolved = "gemini", self.gemini_voice
        self.voice = engine
        if engine == "gemini":
            self.gemini_voice = resolved
        elif engine == "edge":
            self.edge_voice = resolved

    @property
    def levels_available(self) -> bool:
        """True when we play the audio ourselves (and know how loud it is)."""
        return self.voice in ("gemini", "edge")

    def _chain(self) -> list[str]:
        order = {"edge": ["edge", "gemini", "windows"], "gemini": ["gemini", "edge", "windows"]}
        chain = order.get(self.voice, ["windows"])
        return [e for e in chain if e == "windows" or (e != "edge" or _edge_available())]

    def _notice(self, key: str, message: str) -> None:
        if key not in self._notified:
            self._notified.add(key)
            print(message, flush=True)

    # ------------------------------------------------------------ speaking
    def say(self, text: str, *, from_other_thread: bool = False, should_stop=None) -> Spoken:
        text = text.strip()
        if not text or self.voice == "off":
            return Spoken()
        stop = should_stop or (lambda: False)
        started = self._clock()
        self.first_audio_s = None
        played_before = 0.0
        remaining = text
        for engine in self._chain():
            if engine == "windows":
                self.engine_used = "windows"
                spoken = self._windows(remaining, from_other_thread=from_other_thread, stop=stop, started=started)
                spoken.played_s += played_before
                return spoken
            try:
                self.engine_used = engine
                spoken = (self._edge(remaining, stop, started) if engine == "edge"
                          else self._gemini(remaining, stop, started))
                spoken.played_s += played_before
                return spoken
            except _Stopped as exc:
                spoken = exc.args[0]
                spoken.played_s += played_before
                spoken.heard_text = _heard_part(text, spoken.played_s)
                return spoken
            except _Partial as exc:  # failed halfway: say the rest with the next voice, don't go silent
                print(f"(voice {engine} failed partway: {str(exc.cause)[:120]} — finishing with another voice)")
                self.log("voice_failed", f"{engine} failed partway: {str(exc.cause)[:200]}")
                remaining, played_before = exc.remaining, played_before + exc.played_s
            except Exception as exc:  # noqa: BLE001 — never lose the answer because of the voice
                if self.first_audio_s is not None:
                    played = self._clock() - started
                    cut = _heard_part(remaining, played).rstrip("…")
                    remaining = remaining[len(cut):].strip() or remaining
                self.log("voice_failed", f"{engine}: {type(exc).__name__}: {str(exc)[:200]}")
                self._notice(f"{engine}-fail-{type(exc).__name__}",
                             f"({engine} voice unavailable: {str(exc)[:140]} — using another voice)")
        return Spoken()

    # ------------------------------------------------------------ playback
    def _player(self, rate: int, stop, started: float, text: str):
        """Returns play(pcm) → plays in 50 ms blocks, stops instantly when stop() is true."""
        import numpy as np

        sd = _sounddevice()
        state = {"out": None, "played": 0.0}
        block = rate // 20

        def play(pcm) -> None:
            pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
            if state["out"] is None:
                state["out"] = sd.OutputStream(samplerate=rate, channels=1, dtype="int16")
                state["out"].start()
                if self.first_audio_s is None:
                    self.first_audio_s = self._clock() - started
            for i in range(0, len(pcm), block):
                if stop():
                    out = state["out"]
                    getattr(out, "abort", out.stop)()  # drop what's buffered: silence right away
                    raise _Stopped(Spoken(True, state["played"], _heard_part(text, state["played"])))
                chunk = pcm[i:i + block]
                if self.on_level:
                    self.on_level(min(1.0, level(chunk) / 6000))
                state["out"].write(chunk.reshape(-1, 1))
                state["played"] += len(chunk) / rate

        def close() -> float:
            if state["out"] is not None:
                try:
                    state["out"].stop()
                    state["out"].close()
                except Exception:  # noqa: BLE001 — already aborted
                    pass
            return state["played"]

        return play, close

    def _cache_path(self, engine: str, voice: str, text: str):
        if self.cache_dir is None or len(text) > 80:
            return None
        import hashlib
        from pathlib import Path

        key = hashlib.sha1(f"{engine}|{voice}|{_STYLE}|{text}".encode()).hexdigest()[:20]
        return Path(self.cache_dir) / f"{engine}-{voice}" / f"{key}.wav"

    def _cached(self, engine: str, voice: str, text: str, synth):
        """(pcm int16, rate) for text, from the phrase cache when possible."""
        import numpy as np

        path = self._cache_path(engine, voice, text)
        if path is not None and path.exists():
            with wave.open(str(path)) as w:
                return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()
        pcm, rate = synth(text)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(to_wav(np.asarray(pcm, dtype=np.int16).tobytes(), rate=rate))
        return pcm, rate

    # ------------------------------------------------------------ edge (Microsoft neural voices)
    def _edge_synth(self, text: str):
        import asyncio

        import edge_tts
        import numpy as np
        import soundfile

        async def fetch() -> bytes:
            buf = bytearray()
            async for item in edge_tts.Communicate(text, self.edge_voice).stream():
                if item["type"] == "audio":
                    buf += item["data"]
            return bytes(buf)

        mp3 = asyncio.run(asyncio.wait_for(fetch(), timeout=12))
        if not mp3:
            raise RuntimeError("no audio received")
        pcm, rate = soundfile.read(io.BytesIO(mp3), dtype="int16")
        return (pcm if pcm.ndim == 1 else pcm[:, 0]).astype(np.int16), rate

    def _edge(self, text: str, stop, started: float) -> Spoken:
        """Sentence by sentence: the next one is synthesized while the current one plays."""
        from concurrent.futures import ThreadPoolExecutor

        parts = split_for_speech(text)

        def synth(t: str):
            try:
                return self._cached("edge", self.edge_voice, t, self._edge_synth)
            except Exception:  # noqa: BLE001 — one retry: network blips are common
                return self._cached("edge", self.edge_voice, t, self._edge_synth)

        play = close = None
        pool = ThreadPoolExecutor(max_workers=1)
        i = 0
        try:
            pending = pool.submit(synth, parts[0])
            for i in range(len(parts)):
                try:
                    pcm, rate = pending.result()
                except Exception as exc:  # noqa: BLE001
                    if play is None:
                        raise
                    raise _Partial(" ".join(parts[i:]), close(), exc) from exc
                if i + 1 < len(parts):
                    pending = pool.submit(synth, parts[i + 1])
                if play is None:
                    play, close = self._player(rate, stop, started, text)
                play(pcm)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # interrupted: don't wait for the next sentence
            played = close() if close else 0.0
        return Spoken(played_s=played)

    # ------------------------------------------------------------ gemini
    def _gemini(self, text: str, stop, started: float) -> Spoken:
        if self._clock() < self._gemini_off_until:
            raise _QuotaError("daily voice quota used up")
        path = self._cache_path("gemini", self.gemini_voice, text)
        if path is not None:  # short phrase: synthesize once, replay forever
            pcm, rate = self._cached("gemini", self.gemini_voice, text, self._gemini_pcm)
            play, close = self._player(rate, stop, started, text)
            try:
                play(pcm)
            finally:
                played = close()
            return Spoken(played_s=played)
        if self._can_stream:
            try:
                return self._gemini_stream(text, stop, started)
            except (_Stopped, _QuotaError):
                raise
            except Exception:  # noqa: BLE001
                if self.first_audio_s is not None:
                    raise
                self._can_stream = False  # this model/SDK can't stream: whole clips from now on
        pcm, rate = self._gemini_pcm(text)  # one request per reply: one consistent voice
        play, close = self._player(rate, stop, started, text)
        try:
            play(pcm)
        finally:
            played = close()
        return Spoken(played_s=played)

    def _contents(self, text: str) -> str:
        return f"{_STYLE}: {text}" if _STYLE else text

    def _speech_config(self):
        from google.genai import types

        return types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.gemini_voice))))

    def _client(self):
        if self.client is None:
            from .ears import make_client

            self.client = make_client()
        return self.client

    def _models(self) -> list[str]:
        now = self._clock()
        usable = [m for m in self.gemini_models if self._cooled.get(m, 0) <= now]
        if not usable:
            self._gemini_off_until = min(self._cooled.values())
            raise _QuotaError("Gemini voice is over its quota on every speech model")
        return usable

    def _over_quota(self, model: str, exc: Exception) -> bool:
        if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
            self._cooled[model] = self._clock() + self.QUOTA_COOLDOWN_S
            self._notice(f"quota-{model}", f"(Gemini voice {model} hit its quota — trying the next voice option)")
            return True
        return False

    def _gemini_pcm(self, text: str):
        import numpy as np

        last: Exception | None = None
        for model in self._models():
            try:
                response = self._client().models.generate_content(
                    model=model, contents=self._contents(text), config=self._speech_config())
            except Exception as exc:  # noqa: BLE001
                if self._over_quota(model, exc):
                    last = exc
                    continue
                raise
            data = response.candidates[0].content.parts[0].inline_data.data
            if data[:4] == b"RIFF":
                with wave.open(io.BytesIO(data)) as w:
                    return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()
            return np.frombuffer(data[: len(data) - len(data) % 2], dtype=np.int16), 24_000
        raise _QuotaError(str(last))

    def _gemini_stream(self, text: str, stop, started: float) -> Spoken:
        """Play audio as it arrives (raw 24 kHz 16-bit PCM chunks)."""
        import numpy as np

        _sounddevice()  # no audio output → let the caller use whole clips / another engine
        rate = 24_000
        for model in self._models():
            pending = b""
            play = close = None
            try:
                for chunk in self._client().models.generate_content_stream(
                        model=model, contents=self._contents(text), config=self._speech_config()):
                    for cand in chunk.candidates or []:
                        for part in (cand.content.parts if cand.content else None) or []:
                            if part.inline_data and part.inline_data.data:
                                data = part.inline_data.data
                                pending += data[44:] if data[:4] == b"RIFF" else data
                    if play is None and len(pending) < rate // 2:  # ~0.25 s pre-roll against stutter
                        continue
                    if play is None:
                        play, close = self._player(rate, stop, started, text)
                    usable = len(pending) - len(pending) % 2400
                    play(np.frombuffer(pending[:usable], dtype=np.int16))
                    pending = pending[usable:]
                if pending:
                    if play is None:
                        play, close = self._player(rate, stop, started, text)
                    play(np.frombuffer(pending[: len(pending) - len(pending) % 2], dtype=np.int16))
            except Exception as exc:  # noqa: BLE001
                if play is None and self._over_quota(model, exc):
                    continue
                raise
            finally:
                played = close() if close else 0.0
            return Spoken(played_s=played)
        raise _QuotaError("Gemini voice is over its quota")

    # backwards-compatible helper: a whole clip as WAV bytes
    def _gemini_tts(self, text: str) -> bytes:
        pcm, rate = self._gemini_pcm(text)
        return to_wav(pcm.tobytes(), rate=rate)

    # ----------------------------------------------------------- windows
    def _windows(self, text: str, *, from_other_thread: bool, stop, started: float) -> Spoken:
        if sys.platform != "win32":
            print(f"🔊 {text}")
            return Spoken()
        sapi = None if from_other_thread else self._sapi()
        if sapi is None:
            # Text goes in on stdin, never into the command line, so nothing in it can run as code.
            self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SAY],
                        input=text, text=True, encoding="utf-8", capture_output=True)
            return Spoken(played_s=self._clock() - started)
        sapi.Speak(text, 1)  # async, so we can stop it
        self.first_audio_s = self._clock() - started
        while not sapi.WaitUntilDone(50):
            if stop():
                sapi.Speak("", 3)  # async + purge: stops at once
                played = self._clock() - started
                return Spoken(True, played, _heard_part(text, played))
        return Spoken(played_s=self._clock() - started)

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
    """JARVIS_VOICE overrides the engine; "auto" lets the profile's voice decide (Ava → edge, Aoede → gemini)."""
    return os.environ.get("JARVIS_VOICE", "auto")


# ---------------------------------------------------------------- interrupting (barge-in)
class BargeDetector:
    """Decides, chunk by chunk, whether someone started talking over Jarvis.

    Jarvis's own voice also reaches the microphone (echo), often half a second or more *after* we
    play it (Bluetooth, sound "enhancements", driver buffers). So:
    - for the first 1.5 s of a reply it only learns how loud that echo is (the largest echo-to-output
      ratio it sees, with the output level taken over the last second to cover the delay);
    - after that it needs ~0.5 s of speech clearly louder than the echo (3x) before it interrupts;
    - the wake word always interrupts;
    - with a voiceprint, a candidate must also sound like the enrolled voice.
    With headphones there's no echo at all, so talking over Jarvis works best there.
    """

    LEARN_CHUNKS = 19  # ~1.5 s
    MARGIN = 3.0

    def __init__(self, *, noise_floor: float = 300.0, energy: bool = True, verify=None, wake_score=None,
                 wake_threshold: float = 0.5, min_chunks: int = 6, verify_chunks: int = 12):
        self.noise_floor = noise_floor
        self.energy = energy
        self.verify = verify  # samples → True / False / None (can't tell)
        self.wake_score = wake_score
        self.wake_threshold = wake_threshold
        self.min_chunks = min_chunks
        self.verify_chunks = verify_chunks
        self.echo_ratio = 0.0
        self._seen = 0
        self._loud = 0
        self.history: list = []  # recent chunks (pre-roll)
        self.candidate: list = []
        self.reason = ""

    def feed(self, chunk, out_level: float, speech: bool | None = None) -> bool:
        """out_level: loudness we played recently (RMS, int16 scale); speech: voice detector verdict."""
        import numpy as np

        mic = level(chunk)
        self.history = (self.history + [chunk])[-12:]
        if self.wake_score is not None and self.wake_score(chunk) >= self.wake_threshold:
            self.reason = "wake word"
            self.candidate = list(self.history)
            return True
        if not self.energy:
            return False
        self._seen += 1
        if self._seen <= self.LEARN_CHUNKS:  # learning the echo: never interrupt yet
            if out_level > 300:
                self.echo_ratio = max(self.echo_ratio, mic / out_level)
            return False
        expected_echo = self.echo_ratio * out_level
        margin = 1.5 if self.verify else self.MARGIN  # a voiceprint check filters echo anyway
        loud = mic > max(self.noise_floor * 3, 400) and mic > margin * expected_echo + 200 and speech is not False
        if not self.candidate:
            self._loud = self._loud + 1 if loud else 0
            if self._loud < self.min_chunks:
                return False
            self.candidate = list(self.history)  # includes the start of the word
        else:
            self.candidate.append(chunk)
        if self.verify is None:
            self.reason = "voice"
            return True
        if len(self.candidate) < self.verify_chunks:
            return False
        verdict = self.verify(np.concatenate(self.candidate))
        if verdict:
            self.reason = "your voice"
            return True
        if verdict is False or len(self.candidate) >= self.verify_chunks * 2:
            self.candidate, self._loud = [], 0  # echo / someone else: keep talking
        return False

    def learn_false_alarm(self, out_level: float) -> None:
        """An interruption turned out to be nothing: raise the echo estimate so it doesn't repeat."""
        if out_level > 300 and self.history:
            self.echo_ratio = max(self.echo_ratio, max(level(c) for c in self.history) / out_level)


def headphones_in_use() -> bool:
    """Best guess from the default output device's name (Windows names them 'Headphones…',
    'Headset…', 'AirPods…'). Talking over Jarvis is reliable only without speaker echo."""
    try:
        name = str(_sounddevice().query_devices(kind="output")["name"]).lower()
    except Exception:  # noqa: BLE001
        return False
    return any(k in name for k in ("headphone", "headset", "earphone", "earbud", "airpods", "buds", "hands-free"))


class BargeIn:
    """Listens on the microphone while Jarvis speaks. Use as a context manager around Speaker.say."""

    def __init__(self, *, out_level, voiceprint=None, wake=None, energy: bool = True):
        self.out_level = out_level  # () → loudness being played (RMS, int16 scale)
        self.voiceprint = voiceprint
        self.wake = wake
        self.energy = energy
        self.triggered = threading.Event()
        self._stream = None
        self._thread = None
        self._chunks: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self.detector: BargeDetector | None = None
        self.after: list = []

    def __enter__(self):
        try:
            sd = _sounddevice()
            self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                          callback=lambda data, *_: self._chunks.put(data[:, 0].copy()))
            self._stream.start()
        except Exception:  # noqa: BLE001 — no mic: speaking still works, just not interruptible by voice
            self._stream = None
            return self
        verify = None
        if self.voiceprint is not None and self.voiceprint.enrolled:
            verify = self.voiceprint.score_ok
        self.detector = BargeDetector(energy=self.energy, verify=verify,
                                      wake_score=self.wake.score if self.wake else None)
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _watch(self) -> None:
        vad = SpeechVAD.shared()
        vad.reset()
        while not self._stop.is_set():
            try:
                chunk = self._chunks.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.triggered.is_set():
                self.after.append(chunk)
                continue
            if self.detector.feed(chunk, self.out_level(), vad.is_speech(chunk)):
                print(f"\n✋ Interrupted ({self.detector.reason})", flush=True)
                self.triggered.set()

    def collect(self, *, max_s: float = 20.0) -> bytes | None:
        """After an interruption: keep recording until the user pauses; returns WAV (with the start)."""
        import time

        import numpy as np

        if not self.triggered.is_set() or self.detector is None:
            return None
        detector = SilenceDetector(noise_floor=self.detector.noise_floor, max_s=max_s, no_speech_s=max_s)
        detector._speech_started = True  # they're already talking
        vad = SpeechVAD.shared()
        frames = list(self.detector.candidate)
        seen = 0
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            if seen < len(self.after):
                chunk = self.after[seen]
                seen += 1
                frames.append(chunk)
                if detector.feed(level(chunk), speech=vad.is_speech(chunk)):
                    break
            else:
                time.sleep(0.02)
        return to_wav(np.concatenate(frames).tobytes()) if frames else None

    def __exit__(self, *exc):
        self._stop.set()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        return False

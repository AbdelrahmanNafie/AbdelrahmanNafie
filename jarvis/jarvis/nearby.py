"""Answer anyone who is close to the laptop; ignore voices from across the room.

With one microphone, distance shows up mainly as loudness: every doubling of distance costs
about 6 dB, and a voice across the room arrives 15-25 dB quieter than one at the desk (and with
more room echo). So Jarvis measures how loud the *speech* in each recording is and compares it
with how loud requests at the desk usually are (learned automatically from requests it answered).

- Not calibrated yet: speech must stand clearly above the room noise (12 dB).
- Calibrated: speech must be within 12 dB of your usual level (roughly up to 3-4x your normal
  distance). Tune with JARVIS_NEAR_MARGIN (bigger = accepts voices further away).
- Right after "Hey Jarvis" it's 6 dB more lenient: whoever said the wake word wants an answer.
A loud voice far away and a whisper close by can still fool it: it's a loudness rule, not radar.
"""

from __future__ import annotations

import os

import numpy as np

KEY = "near_speech_db"


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def speech_levels(samples: np.ndarray, frame: int = 480) -> dict:
    """Loudness of the speech vs the background in a recording (dB relative to full scale)."""
    n = len(samples) // frame
    if n < 5:
        return {"speech_db": -120.0, "noise_db": -120.0, "voiced_s": 0.0}
    frames = samples[: n * frame].reshape(n, frame).astype(np.float64)
    db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) / 32768 + 1e-9)
    noise = float(np.percentile(db, 10))
    speech = float(np.percentile(db, 90))
    voiced = float(np.sum(db > noise + 10) * frame / 16_000)
    return {"speech_db": round(speech, 1), "noise_db": round(noise, 1), "voiced_s": round(voiced, 2)}


class NearbyGate:
    def __init__(self, store, *, margin_db: float | None = None, min_snr_db: float | None = None):
        self.store = store
        self.margin_db = _env("JARVIS_NEAR_MARGIN", 12.0) if margin_db is None else margin_db
        self.min_snr_db = _env("JARVIS_NEAR_SNR", 12.0) if min_snr_db is None else min_snr_db
        self.last: dict | None = None

    @property
    def usual_db(self) -> float | None:
        saved = self.store.get(KEY)
        try:
            return float(saved[0]) if saved else None
        except ValueError:
            return None

    def check(self, samples: np.ndarray, *, lenient: bool = False) -> tuple[bool, dict]:
        """(close enough?, measurements). Very short sounds pass: there's too little to judge."""
        info = speech_levels(samples)
        usual = self.usual_db
        extra = 6.0 if lenient else 0.0
        if len(samples) < 0.6 * 16_000:
            ok, why = True, "too short to judge"
        elif usual is None:
            need = self.min_snr_db - extra
            ok = info["speech_db"] - info["noise_db"] >= need
            why = f"speech {info['speech_db'] - info['noise_db']:.0f} dB above the room (needs {need:.0f})"
        else:
            floor = usual - self.margin_db - extra
            ok = info["speech_db"] >= floor
            why = f"speech {info['speech_db']:.0f} dB (nearby is about {usual:.0f}, accepts down to {floor:.0f})"
        info.update(ok=ok, why=why)
        self.last = info
        return ok, info

    def learn(self, info: dict | None = None) -> None:
        """A request at this level was real and answered: update the usual nearby level."""
        info = info or self.last
        if not info or not info.get("ok") or info.get("voiced_s", 0) < 0.3:
            return
        usual = self.usual_db
        new = info["speech_db"] if usual is None else 0.85 * usual + 0.15 * info["speech_db"]
        self.store.put(KEY, f"{new:.1f}")

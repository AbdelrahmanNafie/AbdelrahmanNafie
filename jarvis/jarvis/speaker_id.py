"""Is this my voice? Local speaker verification, so Jarvis ignores other people around you.

- Enroll once (`python -m jarvis.enroll`): you read a few sentences, and a voiceprint (a 256-number
  summary of how your voice sounds) is saved to JARVIS_HOME/voiceprint.npy.
- After that, every utterance is compared with the voiceprint before anything is sent to Gemini.
  Other people's voices, the TV and Jarvis's own voice (echo from the speakers) don't match.

Model: WeSpeaker ResNet34 trained on VoxCeleb (CC BY 4.0), as an ONNX file from the sherpa-onnx
release (~26 MB, downloaded once, checked against a fixed SHA-256). Runs offline on the CPU in
a few tens of milliseconds; audio never leaves the laptop for this.
"""

from __future__ import annotations

import hashlib
import io
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np

MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "wespeaker_en_voxceleb_resnet34_LM.onnx")
MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
RATE = 16_000
DEFAULT_THRESHOLD = 0.45  # cosine similarity; same speaker on the same mic is usually well above this


def _threshold() -> float:
    try:
        return float(os.environ.get("JARVIS_VOICE_MATCH", DEFAULT_THRESHOLD))
    except ValueError:
        return DEFAULT_THRESHOLD


# ------------------------------------------------------------------ features (Kaldi-style fbank)
def _mel_banks(num_bins: int, n_fft: int, rate: int, low: float = 20.0, high: float = 0.0) -> np.ndarray:
    high = high if high > 0 else rate / 2
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)  # noqa: E731
    mel_low, mel_high = mel(low), mel(high)
    centers = np.linspace(mel_low, mel_high, num_bins + 2)
    fft_mels = mel(np.arange(n_fft // 2) * rate / n_fft)  # Kaldi ignores the Nyquist bin
    banks = np.zeros((num_bins, n_fft // 2 + 1), dtype=np.float32)
    for i in range(num_bins):
        left, center, right = centers[i], centers[i + 1], centers[i + 2]
        up = (fft_mels - left) / (center - left)
        down = (right - fft_mels) / (right - center)
        banks[i, : n_fft // 2] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks(80, 512, RATE)


def fbank(samples: np.ndarray) -> np.ndarray:
    """80-dim log mel filterbank, 25 ms frames every 10 ms, Hamming window — matching WeSpeaker's
    Kaldi settings (dither 0, pre-emphasis 0.97, DC removal, snip edges). samples: int16 scale."""
    x = samples.astype(np.float64)
    frame_len, shift = 400, 160
    if len(x) < frame_len:
        return np.zeros((0, 80), dtype=np.float32)
    n = 1 + (len(x) - frame_len) // shift
    idx = np.arange(frame_len)[None, :] + shift * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= 0.97 * frames[:, :-1]
    frames[:, 0] -= 0.97 * frames[:, 0]
    frames *= np.hamming(frame_len)[None, :]  # 0.54 - 0.46 cos(2πn/(N-1)), as in Kaldi
    power = np.abs(np.fft.rfft(frames, n=512)) ** 2
    feats = np.log(np.maximum(power @ _BANKS.T, np.finfo(np.float32).eps))
    return feats.astype(np.float32)


# ------------------------------------------------------------------ model
def model_path(home: Path) -> Path:
    return home / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"


def ensure_model(home: Path, *, download=urllib.request.urlretrieve) -> Path:
    path = model_path(home)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    print("Downloading the voice model (26 MB, once)…", flush=True)
    download(MODEL_URL, tmp)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("The downloaded voice model didn't match its checksum, so it wasn't used.")
    tmp.replace(path)
    return path


class Embedder:
    def __init__(self, path: Path):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

    def __call__(self, samples: np.ndarray) -> np.ndarray | None:
        feats = fbank(samples)
        if len(feats) < 50:  # under ~0.5 s of audio: not enough to tell voices apart
            return None
        feats = feats - feats.mean(axis=0, keepdims=True)  # per-utterance mean normalization
        emb = self.session.run(None, {"feats": feats[None]})[0][0]
        return emb / (np.linalg.norm(emb) + 1e-9)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


# ------------------------------------------------------------------ voice activity: keep the speech
def voiced(samples: np.ndarray, frame: int = 480, keep_db: float = 30.0) -> np.ndarray:
    """Drop silent stretches (they dilute the voiceprint): keep frames within keep_db of the loudest."""
    n = len(samples) // frame
    if n == 0:
        return samples
    frames = samples[: n * frame].reshape(n, frame).astype(np.float64)
    energy = 10 * np.log10(np.mean(frames ** 2, axis=1) + 1e-9)
    keep = energy >= energy.max() - keep_db
    keep &= energy > 10 * np.log10(150.0 ** 2)  # absolute floor: below this is room noise
    return frames[keep].astype(np.int16).reshape(-1) if keep.any() else samples[:0]


def wav_samples(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16 kHz mono 16-bit audio")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


# ------------------------------------------------------------------ the voiceprint
class VoicePrint:
    """Compares audio with the enrolled voice. Without an enrollment, everything passes."""

    def __init__(self, home: Path, *, embedder=None, threshold: float | None = None):
        self.home = home
        self.path = home / "voiceprint.npy"
        self.print_: np.ndarray | None = np.load(self.path) if self.path.exists() else None
        self._embedder = embedder
        self.threshold = _threshold() if threshold is None else threshold
        self.last_score: float | None = None

    @property
    def enrolled(self) -> bool:
        return self.print_ is not None

    def embedder(self):
        if self._embedder is None:
            self._embedder = Embedder(ensure_model(self.home))
        return self._embedder

    def score(self, samples: np.ndarray) -> float | None:
        """Similarity with the enrolled voice (about -1..1), or None if there's too little speech."""
        if self.print_ is None:
            return None
        emb = self.embedder()(voiced(samples))
        self.last_score = None if emb is None else cosine(emb, self.print_)
        return self.last_score

    def is_user(self, samples: np.ndarray) -> bool:
        """True if it's the enrolled voice — or if we can't tell (not enrolled, too short)."""
        s = self.score(samples)
        return s is None or s >= self.threshold

    def score_ok(self, samples: np.ndarray) -> bool | None:
        """True / False, or None when there's too little speech to tell."""
        s = self.score(samples)
        return None if s is None else s >= self.threshold

    def matches(self, samples: np.ndarray, window_s: float = 1.5) -> bool:
        """Is the enrolled voice in this recording? Also true if only part of it is you (others
        talking around you): the best 1.5 s window counts. True when not enrolled."""
        if self.print_ is None:
            return True
        best = self.score(samples)
        if best is not None and best >= self.threshold:
            return True
        win, hop = int(window_s * RATE), int(window_s * RATE / 2)
        for start in range(0, max(0, len(samples) - win) + 1, hop):
            s = self.score(samples[start:start + win])
            if s is not None:
                best = s if best is None else max(best, s)
                if s >= self.threshold:
                    self.last_score = s
                    return True
        self.last_score = best
        return best is None  # too little speech to judge: let it through

    def enroll(self, clips: list[np.ndarray]) -> dict:
        embs = [e for e in (self.embedder()(voiced(c)) for c in clips) if e is not None]
        if len(embs) < 3:
            raise ValueError("I need at least 3 clear recordings of your voice.")
        mean = np.mean(embs, axis=0)
        mean /= np.linalg.norm(mean)
        self_scores = [cosine(e, mean) for e in embs]
        np.save(self.path, mean.astype(np.float32))
        self.print_ = mean
        return {"clips": len(embs), "self_match_min": round(min(self_scores), 2), "threshold": self.threshold}

    def forget(self) -> None:
        self.path.unlink(missing_ok=True)
        self.print_ = None

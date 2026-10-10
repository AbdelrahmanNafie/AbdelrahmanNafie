"""Screen awareness.

- active_window(): title of the window in front ("YouTube - Google Chrome"). Cheap, so it
  is added to every request as context.
- capture(): a downscaled JPEG screenshot, taken only when the brain calls look_at_screen
  (e.g. "what's this?", "summarize what's on my screen"). It is sent to Gemini.
"""

from __future__ import annotations

import io
import sys

MAX_SIDE = 1600  # enough to read text, small enough to be fast
JPEG_QUALITY = 70


def active_window() -> str:
    """Title of the foreground window, or "" if unknown."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()
    except Exception:  # noqa: BLE001 — context is a nice-to-have
        return ""


def _grab():
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError('Screen reading needs: pip install -e ".[screen]"') from exc
    return ImageGrab.grab()


def capture(grab=_grab) -> bytes:
    """Screenshot of the main screen as JPEG bytes, longest side at most MAX_SIDE."""
    image = grab().convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()

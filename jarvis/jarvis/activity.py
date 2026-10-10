"""Workflow awareness: what you work on, learned from the window in front. Local only.

- Tracker: every 15 s, notes the app and window title in front. No keystrokes, no screenshots.
  It skips private/incognito windows and password managers, and pauses while you're away.
  Samples are merged into time spans in the local SQLite file, which keeps 30 days.
- summarize() / today_line(): per-app time and main windows (for the prompt and the my_activity tool).
- learn_workflow(): once a day Gemini turns the last 7 days into a few lines about how you work
  (window titles are sent to Gemini for this; turn tracking off with "stop tracking my activity").
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections import defaultdict
from typing import Any, Callable

SAMPLE_S = 15.0
IDLE_AFTER_S = 300.0  # no mouse/keyboard for 5 min = away
KEEP_DAYS = 30
PRIVATE = re.compile(r"inprivate|incognito|private browsing|1password|bitwarden|keepass|lastpass|"
                     r"credential manager", re.I)
APP_NAMES = {
    "code": "VS Code", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "explorer": "File Explorer",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "olk": "Outlook",
    "ms-teams": "Teams", "teams": "Teams", "slack": "Slack", "whatsapp": "WhatsApp", "figma": "Figma",
    "windowsterminal": "Terminal", "powershell": "PowerShell", "cmd": "Command Prompt", "notepad": "Notepad",
    "claude": "Claude", "spotify": "Spotify", "obsidian": "Obsidian", "notion": "Notion", "zoom": "Zoom",
    "acrord32": "Acrobat", "acrobat": "Acrobat", "photoshop": "Photoshop", "devenv": "Visual Studio",
    "pycharm64": "PyCharm", "idea64": "IntelliJ", "telegram": "Telegram", "discord": "Discord",
}


# ------------------------------------------------------------------ sampling (Windows)
def foreground() -> tuple[str, str] | None:
    """(process name, window title) of the window in front, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        exe = ""
        if handle:
            size = wintypes.DWORD(512)
            path = ctypes.create_unicode_buffer(512)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                exe = path.value.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
            kernel32.CloseHandle(handle)
        return exe, buf.value.strip()
    except Exception:  # noqa: BLE001 — awareness is a nice-to-have
        return None


def idle_seconds() -> float:
    """Seconds since the last mouse/keyboard input (0 if unknown)."""
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0.0


def clean(app: str, title: str) -> tuple[str, str]:
    name = APP_NAMES.get(app.lower(), app or "Unknown")
    if PRIVATE.search(title) or PRIVATE.search(app):
        title = "(private)"
    return name, " ".join(title.split())[:120]


class Tracker:
    def __init__(self, store, *, sample: Callable[[], tuple[str, str] | None] = foreground,
                 idle: Callable[[], float] = idle_seconds, enabled: Callable[[], bool] = lambda: True,
                 every_s: float = SAMPLE_S):
        self.store, self.sample, self.idle, self.enabled, self.every_s = store, sample, idle, enabled, every_s

    def tick(self, now: float | None = None) -> bool:
        """Record what's in front. False if nothing was recorded (off, away, unknown)."""
        if not self.enabled() or self.idle() >= IDLE_AFTER_S:
            return False
        fg = self.sample()
        if not fg or not (fg[0] or fg[1]):
            return False
        app, title = clean(*fg)
        self.store.log_activity(app, title, time.time() if now is None else now, max_gap_s=self.every_s * 3)
        return True

    def run(self, stop: threading.Event) -> None:
        self.store.prune_activity(time.time() - KEEP_DAYS * 86400)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — never take Jarvis down
                print(f"(activity tracking failed: {exc})")
            stop.wait(self.every_s)


# ------------------------------------------------------------------ summaries
def summarize(rows: list[dict[str, Any]], since: float, until: float) -> dict[str, Any]:
    """Minutes per app (largest first) with the windows that took most of that time."""
    per_app: dict[str, float] = defaultdict(float)
    per_title: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        start, end = max(r["start_ts"], since), min(r["end_ts"] + SAMPLE_S, until)  # a sample covers ~15 s
        if end <= start:
            continue
        per_app[r["app"]] += end - start
        per_title[r["app"]][r["title"]] += end - start
    apps = []
    for app, secs in sorted(per_app.items(), key=lambda kv: -kv[1]):
        top = sorted(per_title[app].items(), key=lambda kv: -kv[1])[:3]
        apps.append({"app": app, "minutes": round(secs / 60), "top_windows": [t for t, _ in top if t]})
    return {"total_minutes": round(sum(per_app.values()) / 60), "apps": apps}


def _fmt(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def today_line(store, now: float | None = None, max_apps: int = 5) -> str:
    """One compact line for the prompt: today's main apps and the current window."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    rows = store.activity(midnight, now)
    if not rows:
        return ""
    s = summarize(rows, midnight, now)
    parts = [f"{a['app']} {_fmt(a['minutes'])}" + (f" ({a['top_windows'][0][:50]})" if a["top_windows"] else "")
             for a in s["apps"][:max_apps] if a["minutes"] >= 1]
    last = rows[-1]
    current = ""
    if now - last["end_ts"] < SAMPLE_S * 4:
        current = f" Right now: {last['app']} — {last['title'][:60]} for {_fmt(round((now - last['start_ts']) / 60))}."
    return ("Today: " + ", ".join(parts) + "." if parts else "") + current


def report(store, days: float = 1, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    since = now - days * 86400
    return summarize(store.activity(since, now), since, now)


# ------------------------------------------------------------------ learning
LEARN_PROMPT = """\
Below is a log of how one person spent time on their laptop over the last days (app, minutes,
main window titles per day). Write 3 to 6 short lines describing their workflow: main projects
and what they're about, which tools they use for what, typical working hours, recurring patterns.
Only state what the data supports. No advice, no greetings, no markdown — one observation per line.

{log}"""


def learn_workflow(store, generate: Callable[[str], str], now: float | None = None, days: int = 7) -> str | None:
    """Ask the model for a short description of how the user works; saved under kv 'workflow'."""
    now = time.time() if now is None else now
    lines = []
    for d in range(days, 0, -1):
        start, end = now - d * 86400, now - (d - 1) * 86400
        s = summarize(store.activity(start, end), start, end)
        if s["total_minutes"] < 10:
            continue
        day = time.strftime("%a %d %b", time.localtime(start + 43200))
        hours = sorted({time.localtime(r["start_ts"]).tm_hour for r in store.activity(start, end)})
        apps = "; ".join(f"{a['app']} {a['minutes']}m [{' | '.join(t[:60] for t in a['top_windows'])}]"
                         for a in s["apps"][:8])
        lines.append(f"{day} (active hours {hours[0]}-{hours[-1] + 1}h): {apps}")
    if not lines:
        return None
    text = generate(LEARN_PROMPT.format(log="\n".join(lines))).strip()
    if text:
        store.put("workflow", text[:1500])
    return text or None

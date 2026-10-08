"""Runtime settings. Everything lives under JARVIS_HOME (default: ~/.jarvis)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Apps the assistant may launch, by spoken name -> executable. Override with
# JARVIS_HOME/apps.json. Only names in this map can ever be opened.
_DEFAULT_APPS_WINDOWS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "file explorer": "explorer.exe",
    "paint": "mspaint.exe",
}
_DEFAULT_APPS_POSIX = {"text editor": "gedit", "calculator": "gnome-calculator"}


@dataclass(frozen=True)
class Settings:
    home: Path
    workspace: Path  # default folder the hands may read/write
    allowed_roots: tuple[Path, ...]  # every folder the hands may touch
    db_path: Path
    apps: dict[str, str]
    gemini_model: str
    gemini_fallbacks: tuple[str, ...]  # tried in order when the main model is busy
    claude_model: str  # alias for the brain; lighter models stretch your plan's usage limit
    # How long a dangerous action waits for a human click. Must stay below the
    # MCP client's tool timeout (~60 s in Claude Code by default), or Claude gives
    # up while the request still looks approvable.
    approval_wait_s: float

    @property
    def pause_flag(self) -> Path:
        return self.home / "PAUSED"

    @property
    def paused(self) -> bool:
        return self.pause_flag.exists()


def load() -> Settings:
    home = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis")).expanduser().resolve()
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    extra = [
        Path(p).expanduser().resolve()
        for p in os.environ.get("JARVIS_ALLOWED_DIRS", "").split(os.pathsep)
        if p.strip()
    ]

    apps_file = home / "apps.json"
    if apps_file.exists():
        apps = {k.lower(): v for k, v in json.loads(apps_file.read_text("utf-8")).items()}
    else:
        apps = dict(_DEFAULT_APPS_WINDOWS if sys.platform == "win32" else _DEFAULT_APPS_POSIX)

    return Settings(
        home=home,
        workspace=workspace,
        allowed_roots=(workspace, *extra),
        db_path=home / "jarvis.db",
        apps=apps,
        gemini_model=os.environ.get("JARVIS_GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallbacks=tuple(m.strip() for m in os.environ.get(
            "JARVIS_GEMINI_FALLBACKS", "gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()),
        claude_model=os.environ.get("JARVIS_CLAUDE_MODEL", "sonnet"),
        approval_wait_s=float(os.environ.get("JARVIS_APPROVAL_WAIT_S", "45")),
    )

"""The hands: plain functions that touch the laptop.

Each one validates its own inputs and never shells out. Paths are confined to
the allowed roots, so a model (or a prompt injection) cannot reach outside.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .config import Settings

MAX_READ_BYTES = 200_000
_SAFE_NOTE_NAME = re.compile(r"^[\w\- .]{1,80}$")


class ActionError(Exception):
    """Raised for invalid input; the message is shown to the brain."""


def _confine(settings: Settings, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = settings.workspace / candidate
    resolved = candidate.resolve()
    if not any(resolved == root or resolved.is_relative_to(root) for root in settings.allowed_roots):
        raise ActionError(f"'{raw}' is outside the allowed folders")
    return resolved


def list_files(settings: Settings, folder: str = ".") -> dict[str, Any]:
    path = _confine(settings, folder)
    if not path.is_dir():
        raise ActionError(f"'{folder}' is not a folder")
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:200]
    return {
        "folder": str(path),
        "entries": [
            {"name": p.name, "type": "file" if p.is_file() else "folder",
             "size": p.stat().st_size if p.is_file() else None}
            for p in entries
        ],
    }


def read_file(settings: Settings, path: str) -> dict[str, Any]:
    target = _confine(settings, path)
    if not target.is_file():
        raise ActionError(f"'{path}' is not a file")
    data = target.read_bytes()[:MAX_READ_BYTES]
    # Returned content is data from disk, never instructions — the brain's
    # system prompt says so, and the policy still gates anything it triggers.
    return {"path": str(target), "content": data.decode("utf-8", errors="replace"),
            "truncated": target.stat().st_size > MAX_READ_BYTES}


def system_info(settings: Settings) -> dict[str, Any]:
    usage = shutil.disk_usage(settings.home)
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.node(),
        "cpu_count": os.cpu_count(),
        "disk_free_gb": round(usage.free / 1e9, 1),
        "disk_total_gb": round(usage.total / 1e9, 1),
    }


def write_note(settings: Settings, name: str, text: str) -> dict[str, Any]:
    if not _SAFE_NOTE_NAME.match(name):
        raise ActionError("note name may only contain letters, digits, spaces, '-', '_' and '.'")
    notes = settings.workspace / "notes"
    notes.mkdir(exist_ok=True)
    target = notes / (name if name.endswith(".md") else f"{name}.md")
    if target.exists():
        raise ActionError(f"note '{target.name}' already exists; choose another name")
    target.write_text(text, encoding="utf-8")
    return {"created": str(target), "chars": len(text)}


Launcher = Callable[[str], None]


def _default_launcher(executable: str) -> None:
    # No shell, fixed executable from the allowlist — arguments never come from the model.
    subprocess.Popen([executable], close_fds=True)


def open_app(settings: Settings, name: str, *, launcher: Launcher = _default_launcher) -> dict[str, Any]:
    key = name.strip().lower()
    exe = settings.apps.get(key)
    if exe is None:
        raise ActionError(f"'{name}' is not in the app allowlist: {sorted(settings.apps)}")
    launcher(exe)
    return {"opened": key, "executable": exe}


def delete_file(settings: Settings, path: str) -> dict[str, Any]:
    target = _confine(settings, path)
    if not target.is_file():
        raise ActionError(f"'{path}' is not a file (folders are never deleted)")
    # Move to a trash folder instead of unlinking, so mistakes are recoverable.
    trash = settings.home / "trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{time.time_ns()}_{target.name}"
    target.replace(dest)
    return {"deleted": str(target), "recoverable_at": str(dest)}


REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "list_files": list_files,
    "read_file": read_file,
    "system_info": system_info,
    "write_note": write_note,
    "open_app": open_app,
    "delete_file": delete_file,
}

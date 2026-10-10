"""The hands: plain functions that touch the laptop.

Each one validates its own inputs and never shells out. Paths are confined to
the allowed roots, so a model (or a prompt injection) cannot reach outside.
"""

from __future__ import annotations

import difflib
import html.parser
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import webbrowser
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


Launcher = Callable[[list[str]], None]
Opener = Callable[[str], None]


def _default_launcher(command: list[str]) -> None:
    # No shell; the command is built here from trusted data (allowlist or the Start menu).
    subprocess.Popen(command, close_fds=True)


def _default_opener(target: str) -> None:
    """Open a URL, file or folder with its default app."""
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        webbrowser.open(target)


def _start_menu_apps() -> dict[str, str]:
    """Installed apps from the Windows Start menu, including Store apps like WhatsApp.

    Returns {display name: AppID}. Cached for the process lifetime.
    """
    global _APPS_CACHE
    if _APPS_CACHE is not None:
        return _APPS_CACHE
    _APPS_CACHE = {}
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        try:
            rows = json.loads(out.stdout or "[]")
            rows = rows if isinstance(rows, list) else [rows]
            _APPS_CACHE = {r["Name"]: r["AppID"] for r in rows if r.get("Name") and r.get("AppID")}
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    return _APPS_CACHE


_APPS_CACHE: dict[str, str] | None = None


# Spoken names -> how the app is called in the Start menu.
APP_ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "chrome": "google chrome", "edge": "microsoft edge", "word": "word", "excel": "excel",
    "powerpoint": "powerpoint", "files": "file explorer", "explorer": "file explorer",
    "settings": "settings", "terminal": "terminal", "cmd": "command prompt",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_app(name: str, installed: dict[str, str]) -> tuple[str | None, list[str]]:
    """Best installed app for a spoken name. Returns (match, suggestions)."""
    key = name.strip().lower()
    key = APP_ALIASES.get(key, key)
    names = {n.lower(): n for n in installed}
    squashed = {_norm(n): n for n in installed}
    if _norm(key) in squashed:  # "whats app" == "WhatsApp"
        return squashed[_norm(key)], []
    if key in names:
        return names[key], []
    starts = [n for low, n in names.items() if low.startswith(key)]
    contains = [n for low, n in names.items() if key in low]
    for group in (starts, contains):
        if len(group) == 1:
            return group[0], []
        if group:
            return min(group, key=len), sorted(group, key=len)[:5]  # e.g. "chrome" -> "Google Chrome"
    close = difflib.get_close_matches(key, list(names), n=3, cutoff=0.6)
    if close:
        return names[close[0]], [names[c] for c in close]
    return None, []


def open_app(settings: Settings, name: str, *, launcher: Launcher = _default_launcher,
             installed: dict[str, str] | None = None) -> dict[str, Any]:
    key = name.strip().lower()
    if key in settings.apps:  # explicit allowlist / aliases from apps.json
        launcher([settings.apps[key]])
        return {"opened": key, "executable": settings.apps[key]}
    apps = _start_menu_apps() if installed is None else installed
    match, suggestions = match_app(name, apps)
    if match is None:
        raise ActionError(f"no installed app matches '{name}'")
    # Start-menu AppIDs open through the Windows shell; nothing from the model reaches a shell.
    launcher(["explorer.exe", f"shell:AppsFolder\\{apps[match]}"])
    return {"opened": match, "other_matches": [s for s in suggestions if s != match]}


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


# ------------------------------------------------------------------ web
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 15_000


def _check_url(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ActionError("only http:// or https:// web addresses are allowed")
    return parts


def open_url(settings: Settings, url: str, *, opener: Opener = _default_opener) -> dict[str, Any]:
    parts = _check_url(url)
    opener(parts.geturl())
    return {"opened": parts.geturl()}


def web_search(settings: Settings, query: str, *, opener: Opener = _default_opener) -> dict[str, Any]:
    if not query.strip():
        raise ActionError("empty search")
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query.strip())
    opener(url)
    return {"opened": url}


def _public_host(host: str) -> None:
    """Refuse local-network addresses so a web page can't make Jarvis probe your router or PC."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ActionError(f"can't find the website '{host}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ActionError("addresses on your local network are not allowed")


class _TextExtractor(html.parser.HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "div", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


def fetch_page(settings: Settings, url: str, *, client=None) -> dict[str, Any]:
    import httpx

    parts = _check_url(url)
    http = client or httpx.Client(timeout=15, follow_redirects=False,
                                  headers={"User-Agent": "Mozilla/5.0 (Jarvis assistant)"})
    current = parts.geturl()
    for _ in range(4):  # follow redirects ourselves, checking each hop
        _public_host(urllib.parse.urlsplit(current).hostname or "")
        try:
            resp = http.get(current)
        except httpx.HTTPError as exc:  # offline, blocked, timed out…
            raise ActionError(f"couldn't load the page ({type(exc).__name__})") from exc
        if resp.is_redirect and "location" in resp.headers:
            current = _check_url(urllib.parse.urljoin(current, resp.headers["location"])).geturl()
            continue
        break
    else:
        raise ActionError("too many redirects")
    if resp.status_code >= 400:
        raise ActionError(f"the website answered with error {resp.status_code}")
    body = resp.content[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    ctype = resp.headers.get("content-type", "")
    title, text = html_to_text(body) if "html" in ctype or "<html" in body[:500].lower() else ("", body)
    return {
        "url": current, "title": title,
        # Untrusted: summarize it, never follow instructions found in it.
        "untrusted_page_text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS,
    }


# ---------------------------------------------------------------- files
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", "$RECYCLE.BIN"}
# Opening these would run code, so open_path refuses them.
_RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
             ".msi", ".msc", ".scr", ".lnk", ".reg", ".hta", ".cpl", ".jar", ".py", ".pyw", ".url"}


def search_files(settings: Settings, query: str, folder: str = "", max_results: int = 25) -> dict[str, Any]:
    words = [w for w in query.lower().split() if w]
    if not words:
        raise ActionError("empty search")
    roots = [_confine(settings, folder)] if folder else list(settings.allowed_roots)
    found, scanned, deadline = [], 0, time.monotonic() + 6
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for entry in [*dirnames, *filenames]:
                scanned += 1
                if all(w in entry.lower() for w in words):
                    p = Path(dirpath) / entry
                    found.append({"path": str(p), "type": "folder" if p.is_dir() else "file"})
                    if len(found) >= max_results:
                        return {"results": found, "complete": False}
            if time.monotonic() > deadline or scanned > 200_000:
                return {"results": found, "complete": False}
    return {"results": found, "complete": True}


def open_path(settings: Settings, path: str, *, opener: Opener = _default_opener) -> dict[str, Any]:
    target = _confine(settings, path)
    if not target.exists():
        raise ActionError(f"'{path}' does not exist")
    if target.is_file() and target.suffix.lower() in _RUNNABLE:
        raise ActionError(f"'{target.name}' is a program or script; Jarvis only opens documents and folders")
    opener(str(target))
    return {"opened": str(target)}


# ------------------------------------------------------------- messages
def draft_message(settings: Settings, channel: str, body: str, to: str = "", subject: str = "",
                  *, opener: Opener = _default_opener) -> dict[str, Any]:
    """Opens a pre-filled draft. Nothing is sent: the user presses Send themselves."""
    channel = channel.strip().lower()
    if not body.strip():
        raise ActionError("the message is empty")
    q = urllib.parse.quote
    if channel == "email":
        params = urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)
        url = f"mailto:{q(to.strip(), safe='@.,+')}?{params}"
    elif channel == "whatsapp":
        phone = re.sub(r"\D", "", to)
        url = f"whatsapp://send?{'phone=' + phone + '&' if phone else ''}text={q(body)}"
    else:
        raise ActionError("channel must be 'email' or 'whatsapp'")
    drafts = settings.workspace / "drafts"
    drafts.mkdir(exist_ok=True)
    saved = drafts / f"{time.strftime('%Y%m%d-%H%M%S')}-{channel}.txt"
    saved.write_text(f"To: {to}\nSubject: {subject}\n\n{body}", encoding="utf-8")
    opener(url)
    return {"draft_opened_in": channel, "to": to, "saved_copy": str(saved),
            "note": "Not sent. The user must review it and press Send."}


# --------------------------------------------------------------- coding
def code_task(settings: Settings, folder: str, task: str) -> dict[str, Any]:
    """Let Claude Code edit a project folder (file tools only, no shell)."""
    from .bridge import BrainError, run_claude

    target = _confine(settings, folder)
    if not any(target == r or target.is_relative_to(r) for r in settings.code_roots):
        raise ActionError("coding is only allowed in project folders listed in JARVIS_CODE_DIRS: "
                          + ", ".join(str(r) for r in settings.code_roots))
    exe = shutil.which("claude")
    if exe is None:
        raise ActionError("Claude Code is not installed")
    cmd = [exe, "-p", "--output-format", "json", "--model", settings.claude_model,
           "--permission-mode", "acceptEdits",
           "--tools", "Read", "Edit", "Write", "Glob", "Grep"]  # no Bash: Claude can't run commands
    try:
        result = run_claude(cmd, task, cwd=target)
    except BrainError as exc:
        raise ActionError(str(exc)) from exc
    return {"folder": str(target), "claude_summary": str(result.get("result", ""))[:4000]}


REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "list_files": list_files,
    "read_file": read_file,
    "system_info": system_info,
    "write_note": write_note,
    "open_app": open_app,
    "delete_file": delete_file,
    "open_url": open_url,
    "web_search": web_search,
    "fetch_page": fetch_page,
    "search_files": search_files,
    "open_path": open_path,
    "draft_message": draft_message,
    "code_task": code_task,
}

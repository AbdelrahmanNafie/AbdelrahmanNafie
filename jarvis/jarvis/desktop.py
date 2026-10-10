"""Hands for apps on screen: see open windows and browser tabs, switch to one, click, type, press keys.

Windows only (ctypes + the built-in UI Automation through Windows PowerShell; nothing to install).
Where to click is decided from a screenshot of the window in front (see vision.py), so it works
in any app, including web apps like WhatsApp Web inside a browser tab.

Safety: typing, keys and clicks are refused when a terminal, the Run box or another
command-running window is in front, so text on a web page can't get commands executed.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

BROWSERS = {"chrome", "msedge", "brave", "firefox", "opera", "vivaldi", "arc", "chromium"}
# Never type or press keys into these: they would run whatever is typed.
BLOCKED_APPS = {"powershell", "pwsh", "cmd", "windowsterminal", "wt", "conhost", "openconsole", "regedit",
                "mmc", "taskmgr", "consent", "wsl", "bash", "putty", "mintty"}
BLOCKED_TITLES = {"run", "تشغيل"}  # the Win+R box

VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "/": 0xBF, ".": 0xBE, ",": 0xBC, "-": 0xBD, "=": 0xBB, ";": 0xBA, "[": 0xDB, "]": 0xDD,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
_EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2E}


class DesktopError(RuntimeError):
    pass


def parse_keys(combo: str) -> list[int]:
    """'ctrl+alt+/' → virtual-key codes. Letters and digits work too ('ctrl+a')."""
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise DesktopError(f"unknown key '{part}'")
    if not codes or len(codes) > 4:
        raise DesktopError("give 1-4 keys, like 'enter' or 'ctrl+a'")
    return codes


@dataclass
class Shot:
    """A screenshot of one window, and how to map its 0-1000 coordinates back to the screen."""

    jpeg: bytes
    left: int
    top: int
    width: int
    height: int

    def to_screen(self, point) -> tuple[int, int]:
        """[y, x] normalized to 0-1000 (Gemini's format) → screen pixels."""
        y, x = float(point[0]), float(point[1])
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise DesktopError("point outside the window")
        return round(self.left + x / 1000 * self.width), round(self.top + y / 1000 * self.height)


_UIA = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]; $T=[System.Windows.Automation.ControlType]
$S=[System.Windows.Automation.TreeScope]; $P=[System.Windows.Automation.PropertyCondition]
$browsers=@('chrome','msedge','brave','firefox','opera','vivaldi','arc','chromium')
$want=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__WANT__'))
$out=@()
$wins=$A::RootElement.FindAll($S::Children, (New-Object $P($A::ControlTypeProperty, $T::Window)))
foreach($w in $wins){
  $procName=(Get-Process -Id $w.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName
  if(-not $procName -or $browsers -notcontains $procName.ToLower()){continue}
  $strip=$w.FindFirst($S::Descendants, (New-Object $P($A::ControlTypeProperty, $T::Tab)))
  if(-not $strip){continue}
  $tabs=$strip.FindAll($S::Descendants, (New-Object $P($A::ControlTypeProperty, $T::TabItem)))
  foreach($t in $tabs){
    $name=$t.Current.Name
    if($want -and $name -eq $want){
      try{ $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); $sel='selected' }
      catch{ $sel='no-pattern' }
      $r=$t.Current.BoundingRectangle
      @{selected=$sel; hwnd=$w.Current.NativeWindowHandle; x=[int]($r.X+$r.Width/2); y=[int]($r.Y+$r.Height/2)} | ConvertTo-Json -Compress
      exit 0
    }
    $out+=@{hwnd=$w.Current.NativeWindowHandle; app=$procName.ToLower(); window=$w.Current.Name; tab=$name}
  }
}
if($want){ '{"selected":"not-found"}' } else { ConvertTo-Json -Compress -InputObject @($out) }
"""


class Desktop:
    """Real Windows implementation. Tests use a fake with the same methods."""

    def __init__(self, *, run=subprocess.run, sleep=time.sleep):
        self.run = run
        self.sleep = sleep
        if sys.platform == "win32":
            try:  # screen pixels must match window coordinates on scaled (125%/150%) displays
                import ctypes

                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — already set (e.g. by the window library)
                pass

    # ------------------------------------------------------------ finding things
    def windows(self) -> list[dict[str, Any]]:
        """Visible top-level windows with a title: [{hwnd, app, title}]."""
        if sys.platform != "win32":
            return []
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        found: list[dict[str, Any]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # skip owned popups
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = ""
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            if handle:
                size = wintypes.DWORD(512)
                path = ctypes.create_unicode_buffer(512)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    exe = path.value.rsplit("\\", 1)[-1].rsplit(".", 1)[0].lower()
                kernel32.CloseHandle(handle)
            if exe not in ("textinputhost", "shellexperiencehost", "searchhost", "startmenuexperiencehost"):
                found.append({"hwnd": int(hwnd), "app": exe, "title": buf.value})
            return True

        user32.EnumWindows(each, 0)
        return found

    def _uia(self, want: str = "") -> Any:
        import base64

        # The tab name goes in as base64 data and the script as -EncodedCommand: quotes, Arabic or
        # anything else in a tab title can never become PowerShell code.
        script = _UIA.replace("__WANT__", base64.b64encode(want.encode("utf-8")).decode())
        encoded = base64.b64encode(("[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script)
                                   .encode("utf-16-le")).decode()
        proc = self.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
        try:
            return json.loads(proc.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise DesktopError(f"couldn't read the browser tabs: {proc.stderr.strip()[:200]}") from exc

    def browser_tabs(self) -> list[dict[str, Any]]:
        """Every tab of every open browser window: [{hwnd, app, window, tab}]."""
        if sys.platform != "win32":
            return []
        tabs = self._uia()
        return [tabs] if isinstance(tabs, dict) else list(tabs)

    def find(self, name: str) -> dict[str, Any] | None:
        """A browser tab or window whose name contains `name` (tabs first: a tab may not be in front)."""
        key = name.strip().lower()
        for t in self.browser_tabs():
            if key in t["tab"].lower():
                return {"kind": "tab", "hwnd": t["hwnd"], "tab": t["tab"], "label": f"{t['tab']} tab ({t['app']})"}
        for w in self.windows():
            if key in w["title"].lower() or key == w["app"]:
                return {"kind": "window", "hwnd": w["hwnd"], "label": f"{w['title']} ({w['app']})"}
        return None

    # ------------------------------------------------------------ acting
    def activate(self, target: dict[str, Any]) -> None:
        if target["kind"] == "tab":
            result = self._uia(target["tab"])
            if result.get("selected") == "not-found":
                raise DesktopError(f"the tab '{target['tab']}' closed")
            self.focus(target["hwnd"])
            if result.get("selected") != "selected":  # no select pattern: click the tab itself
                self.click(*self._scale(target["hwnd"], result["x"], result["y"]))
        else:
            self.focus(target["hwnd"])
        self.sleep(0.4)

    def _scale(self, hwnd: int, x: int, y: int) -> tuple[int, int]:
        """UI Automation from PowerShell reports unscaled coordinates on a scaled display."""
        import ctypes

        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or 96  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            dpi = 96
        return round(x * dpi / 96), round(y * dpi / 96)

    def focus(self, hwnd: int) -> None:
        import ctypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.keybd_event(0x87, 0, 0, 0)  # a harmless key (F24): Windows lets the last-input app switch focus
        user32.keybd_event(0x87, 0, 2, 0)
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(me, fg_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.AttachThreadInput(me, fg_thread, False)
        self.sleep(0.25)
        if user32.GetForegroundWindow() != hwnd:
            raise DesktopError("Windows didn't let me bring that window to the front")

    def foreground(self) -> tuple[int, str, str]:
        """(hwnd, app, title) of the window in front."""
        import ctypes

        from .activity import foreground

        hwnd = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[attr-defined]
        app, title = foreground() or ("", "")
        return int(hwnd), app.lower(), title

    def check_safe(self) -> None:
        _, app, title = self.foreground()
        if app in BLOCKED_APPS or title.strip().lower() in BLOCKED_TITLES:
            raise DesktopError(f"I don't type or press keys into {title or app}: it could run commands")

    def screenshot(self) -> Shot:
        """The window in front, as JPEG (longest side ≤ 1600) plus its position."""
        import ctypes
        from ctypes import wintypes

        from PIL import ImageGrab

        hwnd, _, _ = self.foreground()
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))  # type: ignore[attr-defined]
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        image.thumbnail((1600, 1600))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=75)
        return Shot(buf.getvalue(), left, top, right - left, bottom - top)

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SetCursorPos(int(x), int(y))
        for _ in range(2 if double else 1):
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
        self.sleep(0.15)

    def keys(self, combo: str) -> None:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        codes = parse_keys(combo)
        for vk in codes:
            user32.keybd_event(vk, 0, 1 if vk in _EXTENDED else 0, 0)
        for vk in reversed(codes):
            user32.keybd_event(vk, 0, (1 if vk in _EXTENDED else 0) | 2, 0)
        self.sleep(0.12)

    def type(self, text: str) -> None:
        """Paste the text (works for Arabic and emoji, unlike simulated key presses)."""
        previous = _clipboard_get()
        _clipboard_set(text)
        self.keys("ctrl+v")
        self.sleep(0.25)
        if previous is not None:
            _clipboard_set(previous)

    def open_url(self, url: str) -> None:
        import os

        os.startfile(url)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- clipboard (ctypes, fast)
def _clip_api():
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    return ctypes, user32, kernel32


def _open_clipboard(user32) -> bool:
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def _clipboard_get() -> str | None:
    if sys.platform != "win32":
        return None
    ctypes, user32, kernel32 = _clip_api()
    if not _open_clipboard(user32):
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_set(text: str) -> None:
    ctypes, user32, kernel32 = _clip_api()
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard(user32):
        raise DesktopError("the clipboard is busy")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):
            raise DesktopError("couldn't put the text on the clipboard")
    finally:
        user32.CloseClipboard()


def main() -> int:
    """`python -m jarvis.desktop` — check that Jarvis can see your windows and browser tabs."""
    desk = Desktop()
    print("Windows:")
    for w in desk.windows():
        print(f"  [{w['app']}] {w['title']}")
    print("\nBrowser tabs:")
    try:
        tabs = desk.browser_tabs()
    except DesktopError as exc:
        print(f"  ❌ {exc}")
        return 1
    for t in tabs:
        print(f"  [{t['app']}] {t['tab']}")
    found = desk.find("whatsapp")
    print(f"\nWhatsApp: {'✅ ' + found['label'] if found and found['kind'] == 'tab' else '❌ no WhatsApp Web tab found'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

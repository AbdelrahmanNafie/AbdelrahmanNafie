"""Self-check: tests each part of Jarvis separately and says exactly what failed.

  python -m jarvis.doctor
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import anyio

from . import config
from .bridge import BrainError, build_claude_command, hands_server_entry, run_claude
from .store import Store

OK, FAIL, INFO = "✅", "❌", "ℹ️ "


def _step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def check_claude_cli() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        print(f"  {FAIL} 'claude' command not found. Install Claude Code, then open a NEW PowerShell window.")
        return None
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, errors="replace")
    print(f"  {OK} found {exe}  ({version.stdout.strip() or version.stderr.strip()})")
    return exe


def check_hands_server(settings: config.Settings) -> bool:
    from mcp.client.client import Client, StdioServerParameters

    entry = hands_server_entry(settings)
    params = StdioServerParameters(command=entry["command"], args=entry["args"],
                                   env={**os.environ, **entry["env"]})

    async def go() -> list[str]:
        with anyio.fail_after(30):
            async with Client(params) as client:
                return sorted(t.name for t in (await client.list_tools()).tools)

    try:
        tools = anyio.run(go)
    except Exception as exc:  # noqa: BLE001 — report anything to the user
        print(f"  {FAIL} the hands server did not start: {type(exc).__name__}: {exc}")
        return False
    print(f"  {OK} hands server started; tools: {', '.join(tools)}")
    return True


def check_claude_login(exe: str, settings: config.Settings) -> bool:
    cmd = [exe, "-p", "--tools", "", "--output-format", "json", "--model", settings.claude_model]
    try:
        result = run_claude(cmd, "Reply with exactly the word: pong", cwd=settings.home)
    except BrainError as exc:
        print(f"  {FAIL} Claude did not answer.\n" + _indent(str(exc)))
        return False
    print(f"  {OK} Claude answered: {result.get('result', '').strip()[:80]!r}")
    return True


def check_brain_uses_hands(settings: config.Settings, store: Store) -> bool:
    before = len(store.events(limit=100000))
    cmd = build_claude_command(settings, continue_session=False)
    prompt = "Self-test: call the system_info tool once, then reply with only the OS name."
    try:
        result = run_claude(cmd, prompt, cwd=settings.home)
    except BrainError as exc:
        print(f"  {FAIL} Claude could not use the Jarvis tools.\n" + _indent(str(exc)))
        return False
    executed = [e for e in store.events(limit=100000)[before:]
                if e["kind"] == "executed" and e["data"].get("action") == "system_info"]
    if not executed:
        print(f"  {FAIL} Claude answered but never called the tool. Answer: {result.get('result', '')[:200]!r}")
        return False
    print(f"  {OK} Claude called system_info and answered: {result.get('result', '').strip()[:80]!r}")
    return True


def _indent(text: str) -> str:
    return "\n".join("     " + line for line in text.splitlines())


def main() -> int:
    settings = config.load()
    store = Store(settings.db_path)
    print("Jarvis doctor")
    print(f"  Python {sys.version.split()[0]} at {sys.executable}")
    print(f"  JARVIS_HOME = {settings.home}")
    print(f"  Claude model for Jarvis = {settings.claude_model}")

    _step(1, "Claude Code command-line tool")
    exe = check_claude_cli()

    _step(2, "Jarvis hands (MCP server)")
    hands_ok = check_hands_server(settings)

    login_ok = brain_ok = False
    if exe:
        _step(3, "Claude login (simple question, no tools)")
        login_ok = check_claude_login(exe, settings)
        if login_ok and hands_ok:
            _step(4, "Claude using the Jarvis hands")
            brain_ok = check_brain_uses_hands(settings, store)

    _step(5, "Gemini key (only needed for Arabic or voice)")
    has_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    print(f"  {OK if has_key else INFO} {'GEMINI_API_KEY is set' if has_key else 'not set yet (fine for English tests)'}")

    all_ok = bool(exe) and hands_ok and login_ok and brain_ok
    print("\n" + (f"{OK} Everything works. Try: python -m jarvis.bridge --text \"Open notepad\""
                  if all_ok else f"{FAIL} Something failed above. Send a screenshot of this window."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

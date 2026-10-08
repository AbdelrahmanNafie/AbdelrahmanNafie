"""The bridge: ears (Gemini) -> brain (Claude via your subscription, no API key).

Examples:
  python -m jarvis.bridge --text "اعمل نوت اسمها شوبينج فيها لبن وعيش"
  python -m jarvis.bridge --audio command.wav
  python -m jarvis.bridge --mic 5              # record 5 s (needs: pip install .[mic])
  python -m jarvis.bridge --text "..." --to print     # stop after the ears
  python -m jarvis.bridge --print-desktop-config      # snippet for Claude Desktop
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .ears import EarsError, Heard, understand
from .store import Store

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BRAIN_PROMPT = """\
Voice request from the user.
Language: {language}
Original: {original}
English: {english}

Carry it out with the jarvis tools, verify the result, then call reply_to_user."""


def hands_server_entry(settings: config.Settings) -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "jarvis.hands_server"],
        "env": {"JARVIS_HOME": str(settings.home), "PYTHONPATH": str(PROJECT_ROOT)},
    }


def write_mcp_config(settings: config.Settings) -> Path:
    path = settings.home / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"jarvis": hands_server_entry(settings)}}, indent=2), "utf-8")
    return path


def build_claude_command(settings: config.Settings, *, continue_session: bool) -> list[str]:
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("Claude Code CLI not found. Install it and run `claude` once to log in "
                           "with your Claude subscription (no API key needed).")
    cmd = [
        exe, "-p",                     # prompt comes on stdin (safe for Arabic on Windows)
        "--mcp-config", str(write_mcp_config(settings)), "--strict-mcp-config",
        "--tools", "",                 # no built-in tools: the brain can ONLY use jarvis hands
        "--allowedTools", "mcp__jarvis",
        "--output-format", "json",
        "--model", settings.claude_model,
    ]
    if continue_session:
        cmd.append("--continue")       # keep conversational memory between voice commands
    return cmd


class BrainError(RuntimeError):
    """Claude Code ran but reported a problem; the message is meant for humans."""


def run_claude(cmd: list[str], prompt: str, *, cwd: Path, env: dict[str, str] | None = None) -> dict:
    """Run the Claude Code CLI and return its JSON result, raising BrainError with the real reason."""
    proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {}
    if proc.returncode == 0 and result and not result.get("is_error"):
        return result
    raise BrainError(describe_claude_failure(proc.returncode, result, proc.stderr, proc.stdout))


def describe_claude_failure(code: int, result: dict, stderr: str, stdout: str) -> str:
    """Pull the human-readable reason out of a failed run (usage numbers are noise here)."""
    lines = [f"Claude Code exited with code {code}."]
    if result.get("subtype"):
        lines.append(f"  type:    {result['subtype']}")
    if result.get("result"):
        lines.append(f"  message: {str(result['result']).strip()[:800]}")
    for err in result.get("errors") or []:
        lines.append(f"  error:   {str(err)[:400]}")
    for denial in result.get("permission_denials") or []:
        lines.append(f"  blocked tool: {denial.get('tool_name', denial)}")
    if stderr.strip():
        lines.append("  stderr:  " + stderr.strip()[-800:])
    if "limit" in str(result.get("result", "")).lower():
        lines.append("  ➜ Not a Jarvis bug: your Claude plan's usage limit is used up. Wait for the reset "
                     "time above, or upgrade the plan. Claude Desktop, Claude Code and Jarvis share it.")
    if not result and stdout.strip():
        lines.append("  output:  " + stdout.strip()[-800:])
    return "\n".join(lines)


def ask_brain(settings: config.Settings, store: Store, heard: Heard, *, new_session: bool) -> str:
    """Send the request to Claude. Returns the short spoken-style reply."""
    prompt = BRAIN_PROMPT.format(**heard.model_dump())
    marker = settings.home / ".brain_session"
    cmd = build_claude_command(settings, continue_session=marker.exists() and not new_session)
    store.log("brain", "thinking", "Claude is planning the task…")
    last_event = store.last_event_id()
    started = time.monotonic()
    # Let tool calls outlive the approval wait, so Claude gets the real answer.
    env = {**os.environ, "MCP_TOOL_TIMEOUT": str(int((settings.approval_wait_s + 30) * 1000))}
    try:
        result = run_claude(cmd, prompt, cwd=settings.home, env=env)
    except BrainError as exc:
        store.log("brain", "failed", str(exc)[:1000])
        raise
    marker.touch()
    store.log("brain", "done", f"finished in {time.monotonic() - started:.1f}s",
              cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"))
    replies = [e["summary"] for e in store.events(after_id=last_event) if e["kind"] == "reply"]
    return replies[-1] if replies else result.get("result", "")


def hear(settings: config.Settings, store: Store, *, text: str | None = None,
         audio: bytes | None = None, mime: str = "audio/wav") -> Heard | None:
    """Ears step shared by the bridge and the assistant. None = nothing intelligible."""
    started = time.monotonic()
    if text is not None:
        store.log("ears", "heard", "typed input", chars=len(text))
    else:
        store.log("ears", "heard", f"audio input ({len(audio) // 1024} KB)")
    heard = understand(text=text, audio=audio, mime_type=mime, model=settings.gemini_model,
                       fallbacks=settings.gemini_fallbacks)
    took = time.monotonic() - started
    store.log("ears", "understood", heard.english or "(nothing intelligible)",
              original=heard.original, language=heard.language, seconds=round(took, 1))
    print(f"👂 [{heard.language}] {heard.original}\n   → {heard.english}   ⏱ {took:.1f}s")
    return heard if heard.english else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--text", help="typed request (Arabic or English)")
    src.add_argument("--audio", type=Path, help="path to a WAV/MP3/OGG recording")
    src.add_argument("--mic", type=float, nargs="?", const=0, metavar="SECONDS",
                     help="record from the microphone (no number: until you stop talking)")
    parser.add_argument("--to", choices=["claude", "print"], default="claude")
    parser.add_argument("--new-session", action="store_true", help="start a fresh Claude conversation")
    parser.add_argument("--print-desktop-config", action="store_true",
                        help="print the claude_desktop_config.json snippet and exit")
    args = parser.parse_args(argv)

    settings = config.load()
    if args.print_desktop_config:
        print(json.dumps({"mcpServers": {"jarvis": hands_server_entry(settings)}}, indent=2))
        return 0
    if not (args.text or args.audio or args.mic is not None):
        parser.error("give --text, --audio or --mic")

    store = Store(settings.db_path)
    try:
        heard = _listen(args, settings, store)
    except EarsError as exc:
        store.log("ears", "failed", str(exc)[:1000])
        print(f"❌ {exc}")
        return 1
    if heard is None or args.to == "print":
        return 0

    started = time.monotonic()
    try:
        answer = ask_brain(settings, store, heard, new_session=args.new_session)
    except BrainError as exc:
        print(f"❌ {exc}\n\nRun `python -m jarvis.doctor` to check each part step by step.")
        return 1
    print(f"🧠 {answer}   ⏱ {time.monotonic() - started:.1f}s")
    return 0


def _listen(args: argparse.Namespace, settings: config.Settings, store: Store) -> Heard | None:
    if args.text:
        return hear(settings, store, text=args.text)
    if args.audio:
        mime = {".mp3": "audio/mp3", ".ogg": "audio/ogg", ".m4a": "audio/aac"}.get(
            args.audio.suffix.lower(), "audio/wav")
        return hear(settings, store, audio=args.audio.read_bytes(), mime=mime)

    from . import voice

    if args.mic:
        print(f"🎙  Listening for up to {args.mic:g}s (stops when you stop talking)…", flush=True)
    audio = voice.record_until_silence(max_s=args.mic or 30.0,
                                       on_start=lambda: print("🎙  Speak now…", flush=True))
    if audio is None:
        print("🤫 I didn't hear anything.")
        return None
    return hear(settings, store, audio=audio)


if __name__ == "__main__":
    raise SystemExit(main())

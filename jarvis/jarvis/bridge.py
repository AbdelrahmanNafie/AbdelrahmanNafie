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
import io
import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

from . import config
from .ears import Heard, understand
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
    ]
    if continue_session:
        cmd.append("--continue")       # keep conversational memory between voice commands
    return cmd


def ask_brain(settings: config.Settings, store: Store, heard: Heard, *, new_session: bool) -> str:
    prompt = BRAIN_PROMPT.format(**heard.model_dump())
    marker = settings.home / ".brain_session"
    cmd = build_claude_command(settings, continue_session=marker.exists() and not new_session)
    store.log("brain", "thinking", "Claude is planning the task…")
    started = time.monotonic()
    # Let tool calls outlive the approval wait, so Claude gets the real answer.
    env = {**os.environ, "MCP_TOOL_TIMEOUT": str(int((settings.approval_wait_s + 30) * 1000))}
    proc = subprocess.run(cmd, input=prompt, cwd=settings.home, env=env, capture_output=True,
                          text=True, encoding="utf-8")
    if proc.returncode != 0:
        store.log("brain", "failed", (proc.stderr or proc.stdout).strip()[:500])
        raise RuntimeError(f"claude exited with {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    marker.touch()
    result = json.loads(proc.stdout)
    answer = result.get("result", "")
    store.log("brain", "done", f"finished in {time.monotonic() - started:.1f}s",
              cost_usd=result.get("total_cost_usd"), turns=result.get("num_turns"))
    return answer


def record_mic(seconds: float, rate: int = 16_000) -> bytes:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("Microphone support needs: pip install .[mic]") from exc
    print(f"🎙  Listening for {seconds:g}s…", flush=True)
    frames = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="int16")
    sd.wait()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames.tobytes())
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.bridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--text", help="typed request (Arabic or English)")
    src.add_argument("--audio", type=Path, help="path to a WAV/MP3/OGG recording")
    src.add_argument("--mic", type=float, metavar="SECONDS", help="record from the microphone")
    parser.add_argument("--to", choices=["claude", "print"], default="claude")
    parser.add_argument("--new-session", action="store_true", help="start a fresh Claude conversation")
    parser.add_argument("--print-desktop-config", action="store_true",
                        help="print the claude_desktop_config.json snippet and exit")
    args = parser.parse_args(argv)

    settings = config.load()
    if args.print_desktop_config:
        print(json.dumps({"mcpServers": {"jarvis": hands_server_entry(settings)}}, indent=2))
        return 0
    if not (args.text or args.audio or args.mic):
        parser.error("give --text, --audio or --mic")

    store = Store(settings.db_path)
    if args.text:
        store.log("ears", "heard", "typed input", chars=len(args.text))
        heard = understand(text=args.text, model=settings.gemini_model)
    else:
        audio = args.audio.read_bytes() if args.audio else record_mic(args.mic)
        mime = {".mp3": "audio/mp3", ".ogg": "audio/ogg", ".m4a": "audio/aac"}.get(
            args.audio.suffix.lower() if args.audio else ".wav", "audio/wav")
        store.log("ears", "heard", f"audio input ({len(audio) // 1024} KB)")
        heard = understand(audio=audio, mime_type=mime, model=settings.gemini_model)

    store.log("ears", "understood", heard.english or "(nothing intelligible)",
              original=heard.original, language=heard.language)
    print(f"👂 [{heard.language}] {heard.original}\n   → {heard.english}")
    if not heard.english or args.to == "print":
        return 0

    answer = ask_brain(settings, store, heard, new_session=args.new_session)
    print(f"🧠 {answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

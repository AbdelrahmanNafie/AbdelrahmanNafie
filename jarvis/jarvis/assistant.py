"""Jarvis mode: always listening. Say "Hey Jarvis", speak, and hear the answer.

  python -m jarvis.assistant                   # wake word "Hey Jarvis"
  python -m jarvis.assistant --push-to-talk    # press Enter instead of the wake word
  python -m jarvis.assistant --to print        # test without Claude: repeats what it understood
  python -m jarvis.assistant --voice gemini    # natural Gemini voice (default: Windows voice)

Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import time
from typing import Callable

from . import config, voice
from .bridge import BrainError, ask_brain, hear
from .ears import EarsError
from .store import Store


def handle_one(settings: config.Settings, store: Store, speaker: voice.Speaker,
               record: Callable[[], bytes | None], *, to: str, new_session: bool) -> None:
    """One request: listen → understand → (Claude) → speak."""
    audio = record()
    if audio is None:
        speaker.say("I didn't hear anything.")
        return
    try:
        print("💭 Understanding…", flush=True)
        heard = hear(settings, store, audio=audio)
    except EarsError as exc:
        print(f"❌ {exc}")
        speaker.say("Sorry, I couldn't understand that because of a connection problem.")
        return
    if heard is None:
        speaker.say("Sorry, I didn't catch that.")
        return
    if to == "print":
        speaker.say(f"I heard: {heard.english}")
        return

    speaker.say("On it.")
    started = time.monotonic()
    try:
        answer = ask_brain(settings, store, heard, new_session=new_session)
    except BrainError as exc:
        print(f"❌ {exc}")
        speaker.say("Sorry, Claude is not available right now. Check the window for details.")
        return
    print(f"🧠 {answer}   ⏱ {time.monotonic() - started:.1f}s")
    speaker.say(answer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.assistant", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push-to-talk", action="store_true", help="press Enter instead of saying Hey Jarvis")
    parser.add_argument("--to", choices=["claude", "print"], default="claude")
    parser.add_argument("--voice", choices=["windows", "gemini", "off"], default=voice.default_voice())
    parser.add_argument("--sensitivity", type=float, default=0.5,
                        help="wake word threshold 0-1 (lower = triggers more easily)")
    args = parser.parse_args(argv)

    settings = config.load()
    store = Store(settings.db_path)
    speaker = voice.Speaker(args.voice)

    if args.push_to_talk:
        def trigger() -> None:
            input("\n⏎  Press Enter and speak… ")
    else:
        print("Loading the wake word model…", flush=True)
        wake = voice.WakeWord(threshold=args.sensitivity)

        def trigger() -> None:
            print("\n💤 Say \"Hey Jarvis\"…", flush=True)
            score = wake.wait()
            print(f"✨ Heard the wake word ({score:.2f})", flush=True)

    def record() -> bytes | None:
        def ready() -> None:
            print("🎙  Speak now (high beep). I stop 2 s after you finish (low beep).", flush=True)
            voice.chime("start")

        audio = voice.record_until_silence(on_start=ready)
        voice.chime("stop")
        return audio

    print("Jarvis is ready. Stop with Ctrl+C.")
    speaker.say("Jarvis is ready.")
    first = True
    try:
        while True:
            trigger()
            try:
                handle_one(settings, store, speaker, record, to=args.to, new_session=first)
                first = False
            except Exception as exc:  # noqa: BLE001 — keep the assistant alive
                print(f"❌ Unexpected error: {type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

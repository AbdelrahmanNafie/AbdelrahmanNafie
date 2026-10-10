"""Jarvis mode: say "Hey Jarvis" once, then just talk.

  python -m jarvis.assistant                   # wake word, then conversation
  python -m jarvis.assistant --push-to-talk    # press Enter instead of the wake word
  python -m jarvis.assistant --to claude       # send every request to Claude
  python -m jarvis.assistant --to print        # only repeat what it understood (no actions)
  python -m jarvis.assistant --voice gemini    # natural Gemini voice (default: Windows voice)

After each answer Jarvis keeps listening for a follow-up (8 s by default) — no
wake word needed. Stay quiet and it goes back to sleep. Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import time
from typing import Callable

from . import config, voice
from .bridge import BrainError, ask_brain, hear
from .ears import EarsError, Heard
from .gateway import Gateway
from .quick import QuickBrain, QuickError
from .store import Store


def handle_one(settings: config.Settings, store: Store, speaker: voice.Speaker,
               record: Callable[[], bytes | None], *, to: str, new_session: bool,
               quick: QuickBrain | None = None) -> bool:
    """One request: listen → understand + act → speak. Returns False if nobody spoke."""
    audio = record()
    if audio is None:
        return False
    started = time.monotonic()

    if to == "quick" and quick is not None:
        print("💭 Thinking…", flush=True)
        store.log("ears", "heard", f"audio input ({len(audio) // 1024} KB)")
        try:
            said, answer = quick.handle(audio=audio)
        except QuickError as exc:
            print(f"❌ {exc}")
            speaker.say("Sorry, Gemini isn't answering right now. Check the window for details.")
            return True
        if said:
            print(f"👂 {said}")
            store.log("ears", "understood", said)
        store.log("brain", "reply", answer)
        print(f"⚡ {answer}   ⏱ {time.monotonic() - started:.1f}s")
        speaker.say(answer)
        return True

    try:
        print("💭 Understanding…", flush=True)
        heard = hear(settings, store, audio=audio)
    except EarsError as exc:
        print(f"❌ {exc}")
        speaker.say("Sorry, I couldn't understand that because of a connection problem.")
        return True
    if heard is None:
        speaker.say("Sorry, I didn't catch that.")
        return True
    if to == "print":
        speaker.say(f"I heard: {heard.english}")
        return True

    speaker.say("On it.")
    try:
        answer = ask_brain(settings, store, heard, new_session=new_session)
    except BrainError as exc:
        print(f"❌ {exc}")
        speaker.say("Sorry, Claude is not available right now. Check the window for details.")
        return True
    print(f"🧠 {answer}   ⏱ {time.monotonic() - started:.1f}s")
    speaker.say(answer)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.assistant", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push-to-talk", action="store_true", help="press Enter instead of saying Hey Jarvis")
    parser.add_argument("--to", choices=["quick", "claude", "print"], default="quick",
                        help="quick: Gemini does light tasks, hands heavy ones to Claude (default)")
    parser.add_argument("--voice", choices=["windows", "gemini", "off"], default=voice.default_voice())
    parser.add_argument("--sensitivity", type=float, default=0.4,
                        help="wake word threshold 0-1 (lower = triggers more easily)")
    parser.add_argument("--follow-up", type=float, default=8.0, metavar="SECONDS",
                        help="keep listening this long after each answer (0 = wake word every time)")
    args = parser.parse_args(argv)

    settings = config.load()
    store = Store(settings.db_path)
    speaker = voice.Speaker(args.voice)

    quick = None
    if args.to == "quick":
        def announce(what: str) -> None:
            print(f"\n🔐 Approval needed: {what}\n   Press Y to approve, N to cancel (or use the dashboard).",
                  flush=True)
            speaker.say("I need your approval. Press Y to allow, or N to cancel.")

        def remind(message: str) -> None:  # runs on a timer thread
            print(f"\n⏰ Reminder: {message}", flush=True)
            voice.chime("start")
            speaker.say(f"Reminder: {message}", from_other_thread=True)

        gateway = Gateway(settings, store, on_approval_needed=announce, approval_key=voice.yes_no_pressed,
                          notify=remind)

        def claude(task: str) -> str:
            print("   🧠 Handing this to Claude…", flush=True)
            speaker.say("This needs Claude. One moment.")
            return ask_brain(settings, store, Heard(original=task, language="english", english=task),
                             new_session=False)

        quick = QuickBrain(settings, gateway, ask_claude=claude)

    if args.push_to_talk:
        def trigger() -> None:
            input("\n⏎  Press Enter and speak… ")
    else:
        print("Loading the wake word model…", flush=True)
        wake = voice.WakeWord(threshold=args.sensitivity)

        def trigger() -> None:
            print("\n💤 Say \"Hey Jarvis\" (or press Enter)…", flush=True)
            score = wake.wait(should_stop=voice.enter_pressed)
            print("✨ Heard the wake word" + (f" ({score:.2f})" if score is not None else " (Enter)"), flush=True)

    def record(follow_up: bool) -> bytes | None:
        def ready() -> None:
            if follow_up:
                print(f"👂 Listening for a follow-up ({args.follow_up:g} s)… stay quiet to end.", flush=True)
            else:
                print("🎙  Speak now (beep). I stop when you pause.", flush=True)
                voice.chime("start")

        audio = voice.record_until_silence(on_start=ready,
                                           no_speech_s=args.follow_up if follow_up else 8.0)
        if audio is not None:
            voice.chime("stop")
        return audio

    print("Jarvis is ready. Stop with Ctrl+C.")
    speaker.say("Jarvis is ready.")
    first = True
    try:
        while True:
            trigger()
            follow_up = False
            # Conversation: keep going without the wake word until the user goes quiet.
            while True:
                try:
                    spoke = handle_one(settings, store, speaker, lambda: record(follow_up), to=args.to,
                                       new_session=first, quick=quick)
                    first = False
                except Exception as exc:  # noqa: BLE001 — keep the assistant alive
                    print(f"❌ Unexpected error: {type(exc).__name__}: {exc}")
                    spoke = False
                if not spoke:
                    if not follow_up:
                        speaker.say("I didn't hear anything.")
                    break
                if args.follow_up <= 0:
                    break
                follow_up = True
    except KeyboardInterrupt:
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

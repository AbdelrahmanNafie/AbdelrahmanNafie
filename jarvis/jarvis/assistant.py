"""Jarvis mode: say "Hey Jarvis" once, then just talk — with the Jarvis app window.

  python -m jarvis.assistant                   # app window + wake word + conversation
  python -m jarvis.assistant --no-ui           # console only
  python -m jarvis.assistant --push-to-talk    # press Enter instead of the wake word
  python -m jarvis.assistant --to claude       # send every request to Claude
  python -m jarvis.assistant --to print        # only repeat what it understood (no actions)
  python -m jarvis.assistant --voice gemini    # natural Gemini voice (default: Windows voice)

In the window you can also tap the orb to talk, type a request, and allow/deny
actions. After each answer Jarvis keeps listening for a follow-up (8 s by default).
Stop with Ctrl+C or by closing the window.
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import Callable

from . import config, voice
from .bridge import BrainError, ask_brain, hear
from .ears import EarsError, Heard
from .gateway import Gateway
from .quick import QuickBrain, QuickError
from .store import Store
from .ui import UI, NullUI, run_native_window


class _UISpeaker:
    """Wraps the speaker so the orb shows 'speaking' while the voice plays."""

    def __init__(self, speaker: voice.Speaker, ui):
        self.speaker, self.ui = speaker, ui

    def say(self, text: str, **kw) -> None:
        if not text.strip():
            return
        self.ui.emit("state", state="speaking")
        try:
            self.speaker.say(text, **kw)
        finally:
            self.ui.emit("state", state="idle")


def handle_one(settings: config.Settings, store: Store, speaker, record: Callable[[], bytes | None], *,
               to: str, new_session: bool, quick: QuickBrain | None = None, ui=None,
               text: str | None = None) -> bool:
    """One request: listen (or typed text) → understand + act → speak. False if nobody spoke."""
    ui = ui or NullUI()
    if text is None:
        audio = record()
        if audio is None:
            return False
    else:
        audio = None
    started = time.monotonic()
    ui.emit("state", state="thinking")

    if to == "quick" and quick is not None:
        print("💭 Thinking…", flush=True)
        try:
            if audio is not None:
                store.log("ears", "heard", f"audio input ({len(audio) // 1024} KB)")
                said, answer = quick.handle(audio=audio)
            else:
                said, answer = quick.handle(Heard(original=text, language="mixed", english=text))
        except QuickError as exc:
            print(f"❌ {exc}")
            ui.emit("error", text=str(exc))
            speaker.say("Sorry, Gemini isn't answering right now. Check the window for details.")
            return True
        if said:
            print(f"👂 {said}")
            store.log("ears", "understood", said)
            ui.emit("heard", text=said)
        took = time.monotonic() - started
        store.log("brain", "reply", answer)
        print(f"⚡ {answer}   ⏱ {took:.1f}s")
        ui.emit("answer", text=answer, seconds=took)
        speaker.say(answer)
        return True

    try:
        print("💭 Understanding…", flush=True)
        heard = hear(settings, store, text=text) if text is not None else hear(settings, store, audio=audio)
    except EarsError as exc:
        print(f"❌ {exc}")
        ui.emit("error", text=str(exc))
        speaker.say("Sorry, I couldn't understand that because of a connection problem.")
        return True
    if heard is None:
        speaker.say("Sorry, I didn't catch that.")
        return True
    ui.emit("heard", text=heard.original)
    if to == "print":
        ui.emit("answer", text=f"I heard: {heard.english}")
        speaker.say(f"I heard: {heard.english}")
        return True

    speaker.say("On it.")
    ui.emit("state", state="thinking")
    try:
        answer = ask_brain(settings, store, heard, new_session=new_session)
    except BrainError as exc:
        print(f"❌ {exc}")
        ui.emit("error", text=str(exc))
        speaker.say("Sorry, Claude is not available right now. Check the window for details.")
        return True
    took = time.monotonic() - started
    print(f"🧠 {answer}   ⏱ {took:.1f}s")
    ui.emit("answer", text=answer, seconds=took)
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
    parser.add_argument("--no-ui", action="store_true", help="don't open the Jarvis app window")
    args = parser.parse_args(argv)

    settings = config.load()
    store = Store(settings.db_path)
    ui = NullUI() if args.no_ui else UI(store)
    speaker = _UISpeaker(voice.Speaker(args.voice), ui)
    stop = threading.Event()

    quick = None
    if args.to == "quick":
        def announce(what: str) -> None:
            print(f"\n🔐 Approval needed: {what}\n   Press Y to approve, N to cancel (or use the app).", flush=True)
            pending = store.pending_approvals()
            if pending:
                p = pending[-1]
                ui.emit("approval", id=p["id"], action=p["action"], args=p["args"], seconds=settings.approval_wait_s)
            speaker.say("I need your approval.")

        def remind(message: str) -> None:  # runs on a timer thread
            print(f"\n⏰ Reminder: {message}", flush=True)
            ui.emit("reminder", text=message)
            voice.chime("start")
            speaker.say(f"Reminder: {message}", from_other_thread=True)

        gateway = Gateway(settings, store, on_approval_needed=announce, approval_key=voice.yes_no_pressed,
                          notify=remind)

        def claude(task: str) -> str:
            print("   🧠 Handing this to Claude…", flush=True)
            speaker.say("This needs Claude. One moment.")
            ui.emit("state", state="thinking")
            return ask_brain(settings, store, Heard(original=task, language="english", english=task),
                             new_session=False)

        quick = QuickBrain(settings, gateway, ask_claude=claude, on_event=lambda kind, **d: ui.emit(kind, **d))

    def interrupted() -> bool:
        return stop.is_set() or voice.enter_pressed() or ui.has_command()

    if args.push_to_talk and args.no_ui:
        def trigger() -> None:
            input("\n⏎  Press Enter and speak… ")
    elif args.push_to_talk:
        def trigger() -> None:
            print("\n⏎  Tap the orb, type in the app, or press Enter…", flush=True)
            while not interrupted():
                time.sleep(0.05)
    else:
        print("Loading the wake word model…", flush=True)
        wake = voice.WakeWord(threshold=args.sensitivity)

        def trigger() -> None:
            print("\n💤 Say \"Hey Jarvis\" (or press Enter / tap the orb)…", flush=True)
            score = wake.wait(should_stop=interrupted)
            if score is not None:
                print(f"✨ Heard the wake word ({score:.2f})", flush=True)

    def record(follow_up: bool) -> bytes | None:
        def ready() -> None:
            ui.emit("state", state="listening")
            if follow_up:
                print(f"👂 Listening for a follow-up ({args.follow_up:g} s)… stay quiet to end.", flush=True)
            else:
                print("🎙  Speak now (beep). I stop when you pause.", flush=True)
                voice.chime("start")

        audio = voice.record_until_silence(on_start=ready, no_speech_s=args.follow_up if follow_up else 8.0,
                                           on_level=lambda v: ui.emit("level", value=v))
        if audio is not None:
            voice.chime("stop")
        return audio

    def loop() -> None:
        print("Jarvis is ready. Stop with Ctrl+C" + ("" if args.no_ui else " or close the window") + ".")
        speaker.say("Jarvis is ready.")
        first = True
        while not stop.is_set():
            ui.emit("state", state="sleeping")
            trigger()
            if stop.is_set():
                break
            command = ui.next_command()
            follow_up = False
            while not stop.is_set():  # conversation: no wake word until the user goes quiet
                typed = command[1] if command and command[0] == "text" else None
                command = None
                try:
                    spoke = handle_one(settings, store, speaker, lambda: record(follow_up), to=args.to,
                                       new_session=first, quick=quick, ui=ui, text=typed)
                    first = False
                except Exception as exc:  # noqa: BLE001 — keep the assistant alive
                    print(f"❌ Unexpected error: {type(exc).__name__}: {exc}")
                    ui.emit("error", text=f"{type(exc).__name__}: {exc}")
                    spoke = False
                if not spoke:
                    if not follow_up:
                        speaker.say("I didn't hear anything.")
                    break
                if args.follow_up <= 0:
                    break
                follow_up = True
                if ui.has_command():  # typed something while Jarvis was answering
                    command = ui.next_command()

    try:
        if isinstance(ui, UI):
            print(f"Jarvis app: {ui.url}")
            worker = threading.Thread(target=loop, daemon=True)
            worker.start()
            # A native window needs the main thread; otherwise open Edge/Chrome in app mode.
            if not run_native_window(ui, on_closed=stop.set):
                ui.open_window()
                while worker.is_alive():
                    worker.join(0.5)
        else:
            loop()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        print("\nGoodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

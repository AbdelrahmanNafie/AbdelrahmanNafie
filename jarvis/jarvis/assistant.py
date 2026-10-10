"""Jarvis mode: say "Hey Jarvis" once, then just talk — with the Jarvis app window.

  python -m jarvis.assistant                   # app window + wake word + conversation
  python -m jarvis.assistant --no-ui           # console only
  python -m jarvis.assistant --push-to-talk    # press Enter instead of the wake word
  python -m jarvis.assistant --to claude       # send every request to Claude
  python -m jarvis.assistant --to print        # only repeat what it understood (no actions)
  python -m jarvis.assistant --voice windows   # built-in Windows voice (default: natural Gemini voice)

In the window you can also tap the orb to talk, type a request, and allow/deny
actions. After each answer Jarvis keeps listening for a follow-up (8 s by default).
Say "pause" / "go to sleep" to stop the conversation, "restart" to reload (e.g. after it
improved its own code). Stop with Ctrl+C or by closing the window.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import Callable

from . import actions, activity, config, voice
from .profile import Profile
from .bridge import BrainError, ask_brain, hear
from .ears import EarsError, Heard
from .gateway import Gateway
from .quick import QuickBrain, QuickError
from .store import Store
from .ui import UI, NullUI, run_native_window


RESTART_CODE = 3  # the child asks the supervisor to start it again


class _UISpeaker:
    """Wraps the speaker so the orb shows 'speaking' while the voice plays (one voice at a time)."""

    def __init__(self, speaker: voice.Speaker, ui):
        self.speaker, self.ui = speaker, ui
        self._lock = threading.Lock()

    def say(self, text: str, **kw) -> None:
        if not text.strip():
            return
        with self._lock:
            self.ui.emit("state", state="speaking")
            try:
                self.speaker.say(text, **kw)
            finally:
                self.ui.emit("state", state="idle")

    @property
    def first_audio_s(self) -> float | None:
        return getattr(self.speaker, "first_audio_s", None)


class Control:
    """What the brain's go_to_sleep / restart_yourself tools ask the assistant loop to do."""

    def __init__(self):
        self.sleep_requested = threading.Event()
        self.restart_requested = threading.Event()

    def sleep(self) -> None:
        self.sleep_requested.set()

    def restart(self) -> None:
        self.restart_requested.set()


def briefing(profile: Profile, store: Store, now: float | None = None) -> str:
    """Start-up greeting built locally (no Gemini call): name, open tasks, next reminder."""
    now = time.time() if now is None else now
    hour = time.localtime(now).tm_hour
    hello = ("Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 17
             else "Good evening" if 17 <= hour < 23 else "Hi")
    name = profile.user_name
    parts = [f"{hello}{', ' + name if name else ''}. {profile.assistant_name} here."]
    if not name:
        parts.append("I don't know your name yet. Say Hey Jarvis and tell me what to call you.")
    tasks = store.find_records("tasks")
    if tasks:
        overdue = sum(1 for t in tasks if t["due_ts"] and t["due_ts"] < now)
        parts.append(f"You have {len(tasks)} open task{'s' if len(tasks) != 1 else ''}"
                     + (f", {overdue} overdue" if overdue else "") + ".")
    upcoming = [r for r in store.upcoming_reminders(3) if r["due_ts"] >= now]
    if upcoming:
        nxt = upcoming[0]
        parts.append(f"Next reminder at {time.strftime('%H:%M', time.localtime(nxt['due_ts']))}: {nxt['message']}.")
    return " ".join(parts)


class Monitor:
    """Background check every few seconds: due reminders (always), low battery (if proactive),
    and once a day a fresh summary of how the user works (if activity tracking is on)."""

    LEARN_EVERY_S = 20 * 3600

    def __init__(self, settings: config.Settings, store: Store, ui, *, battery=actions._battery,
                 generate: Callable[[str], str] | None = None):
        self.settings, self.store, self.ui, self.battery = settings, store, ui, battery
        self.generate = generate  # text → text with Gemini, for learning the workflow
        self._battery_warned = False
        self._last_learn_try = 0.0

    def maybe_learn(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.generate is None or now - self._last_learn_try < 3600:
            return False
        if not Profile.load(self.settings.home).activity_tracking:
            return False
        known = self.store.get("workflow")
        if known and now - known[1] < self.LEARN_EVERY_S:
            return False
        self._last_learn_try = now
        return activity.learn_workflow(self.store, self.generate, now) is not None

    def check(self, now: float | None = None) -> list[str]:
        """Returns what should be said now."""
        now = time.time() if now is None else now
        said = []
        for rem in self.store.take_due_reminders(now):
            late = now - rem["due_ts"] > 300  # Jarvis wasn't running when it was due
            said.append(f"{'Missed reminder' if late else 'Reminder'}: {rem['message']}")
            self.ui.emit("reminder", text=rem["message"])
        if Profile.load(self.settings.home).proactive:
            b = self.battery() or {}
            if b.get("plugged_in") or b.get("percent") is None:
                self._battery_warned = False
            elif b["percent"] <= 20 and not self._battery_warned:
                self._battery_warned = True
                said.append(f"Heads up, your battery is at {b['percent']} percent. You might want to plug in.")
        return said

    def run(self, say: Callable[[str], None], stop: threading.Event, every_s: float = 15.0) -> None:
        while not stop.is_set():
            try:
                for text in self.check():
                    print(f"\n⏰ {text}", flush=True)
                    voice.chime("start")
                    say(text)
                if self.maybe_learn():
                    print("\n🧭 Updated what I know about how you work.", flush=True)
            except Exception as exc:  # noqa: BLE001 — the monitor must never take Jarvis down
                print(f"(background check failed: {exc})")
            stop.wait(every_s)


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
        print(f"⚡ {answer}", flush=True)
        ui.emit("answer", text=answer, seconds=took)
        speaker.say(answer)
        voice_s = getattr(speaker, "first_audio_s", None)
        print(f"   ⏱ understood + acted in {took:.1f}s" + (f" · voice started {voice_s:.1f}s later" if voice_s else ""))
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
    parser.add_argument("--voice", choices=["gemini", "windows", "off"], default=voice.default_voice(),
                        help="gemini: natural voice from your profile (default); windows: built-in voice")
    parser.add_argument("--sensitivity", type=float, default=0.4,
                        help="wake word threshold 0-1 (lower = triggers more easily)")
    parser.add_argument("--follow-up", type=float, default=8.0, metavar="SECONDS",
                        help="keep listening this long after each answer (0 = wake word every time)")
    parser.add_argument("--no-ui", action="store_true", help="don't open the Jarvis app window")
    args = parser.parse_args(argv)
    if os.environ.get("JARVIS_CHILD") != "1":
        return _supervise(sys.argv[1:] if argv is None else argv)

    settings = config.load()
    store = Store(settings.db_path)
    profile = Profile.load(settings.home)
    ui = NullUI() if args.no_ui else UI(store)
    raw_speaker = voice.Speaker(args.voice, on_level=lambda v: ui.emit("level", value=v, source="voice"))
    if args.voice == "gemini":
        raw_speaker.set_voice(profile.voice)
    speaker = _UISpeaker(raw_speaker, ui)
    ui.emit("profile", assistant_name=profile.assistant_name, user_name=profile.user_name)
    stop = threading.Event()
    control = Control()

    quick = None
    if args.to == "quick":
        def announce(what: str) -> None:
            print(f"\n🔐 Approval needed: {what}\n   Press Y to approve, N to cancel (or use the app).", flush=True)
            pending = store.pending_approvals()
            if pending:
                p = pending[-1]
                ui.emit("approval", id=p["id"], action=p["action"], args=p["args"], seconds=settings.approval_wait_s)
            speaker.say("I need your approval.")

        def on_profile(p: Profile) -> None:  # "call me…", "your name is…", "use a different voice"
            if args.voice != "off":
                raw_speaker.set_voice(p.voice)
            ui.emit("profile", assistant_name=p.assistant_name, user_name=p.user_name)

        gateway = Gateway(settings, store, on_approval_needed=announce, approval_key=voice.yes_no_pressed,
                          control=control, on_profile=on_profile)

        def claude(task: str) -> str:
            print("   🧠 Handing this to Claude…", flush=True)
            speaker.say("This needs Claude. One moment.")
            ui.emit("state", state="thinking")
            rules = [m["text"] for m in store.memories(limit=200) if m["category"] == "instructions"]
            if rules:  # Claude follows the same standing instructions as Gemini
                task += "\n\nThe user's standing instructions (always follow):\n" + "\n".join(f"- {r}" for r in rules)
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
        print(f"{profile.assistant_name} is ready. Stop with Ctrl+C" + ("" if args.no_ui else " or close the window") + ".")
        speaker.say(briefing(profile, store))
        monitor = Monitor(settings, store, ui, generate=_gemini_text(settings) if args.to == "quick" else None)
        threading.Thread(target=monitor.run, args=(lambda t: speaker.say(t, from_other_thread=True), stop),
                         daemon=True).start()
        tracker = activity.Tracker(store, enabled=lambda: Profile.load(settings.home).activity_tracking)
        threading.Thread(target=tracker.run, args=(stop,), daemon=True).start()
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
                if control.restart_requested.is_set():
                    print("\n🔄 Restarting…", flush=True)
                    os._exit(RESTART_CODE)  # also closes the window; the supervisor starts a fresh copy
                if control.sleep_requested.is_set():  # "pause" / "go to sleep": back to the wake word
                    control.sleep_requested.clear()
                    break
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


def _gemini_text(settings: config.Settings) -> Callable[[str], str]:
    def generate(prompt: str) -> str:
        from google.genai import types

        from .ears import make_client

        response = make_client().models.generate_content(
            model=settings.quick_model, contents=prompt, config=types.GenerateContentConfig(temperature=0.2))
        return response.text or ""
    return generate


def _supervise(argv: list[str]) -> int:
    """Run Jarvis as a child process and start it again when it asks to restart.

    A restart loads new code (e.g. after improve_myself) in the same console window.
    """
    env = {**os.environ, "JARVIS_CHILD": "1"}
    while True:
        child = subprocess.Popen([sys.executable, "-m", "jarvis.assistant", *argv], env=env)
        try:
            code = child.wait()
        except KeyboardInterrupt:  # Ctrl+C reaches the child too; let it say goodbye
            try:
                return child.wait(timeout=10)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                child.kill()
                return 130
        if code != RESTART_CODE:
            return code


if __name__ == "__main__":
    raise SystemExit(main())

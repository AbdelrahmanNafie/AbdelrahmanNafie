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
import collections
import os
import random
import re
import subprocess
import sys
import threading
import time
from typing import Callable

from . import actions, activity, config, voice
from .profile import Profile
from .nearby import NearbyGate
from .speaker_id import VoicePrint, wav_samples
from .bridge import BrainError, ask_brain, hear
from .ears import EarsError, Heard
from .gateway import Gateway
from .quick import QuickBrain, QuickError
from .store import Store
from .ui import UI, NullUI, run_native_window


RESTART_CODE = 3  # the child asks the supervisor to start it again


# Said (from the phrase cache: instant and free) when a tool will take a few seconds.
SLOW_TOOLS = {"search_web", "read_web_page", "ask_claude", "code_task", "look_at_screen", "improve_myself",
              "my_activity"}
FILLERS = {
    "english": ["One sec, let me check.", "Let me look into that.", "Give me a moment.", "On it, one second."],
    "arabic": ["ثانية واحدة، هشوف.", "لحظة، بدوّرلك.", "استنى ثانية."],
}


class _UISpeaker:
    """Wraps the speaker: the orb shows 'speaking', one voice at a time, and replies can be
    interrupted (by talking over Jarvis, the wake word, Esc, a tap or typing)."""

    def __init__(self, speaker: voice.Speaker, ui, *, barge=None):
        self.speaker, self.ui = speaker, ui
        self.barge = barge  # text → voice.BargeIn (or None when interrupting by voice is off)
        self._lock = threading.Lock()

    def say(self, text: str, *, interruptible: bool = False, after: str = "idle", **kw):
        if not text.strip():
            return None
        with self._lock:
            self.ui.emit("state", state="speaking")
            try:
                if not interruptible:
                    return self.speaker.say(text, **kw)

                def by_hand() -> bool:
                    return self.ui.has_command() or voice.escape_pressed()

                listener = self.barge(text) if self.barge else None
                if listener is None:
                    return self.speaker.say(text, should_stop=by_hand, **kw)
                with listener as heard:
                    spoken = self.speaker.say(text, should_stop=lambda: heard.triggered.is_set() or by_hand(), **kw)
                    if spoken is not None and heard.triggered.is_set():
                        self.ui.emit("state", state="listening")
                        spoken.audio = heard.collect()
                return spoken
            finally:
                self.ui.emit("state", state=after)

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
               text: str | None = None, outcome: dict | None = None) -> bool:
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
        if not answer:  # only background voices / not meant for Jarvis
            print(f"🙉 Not meant for me{(': ' + said) if said else ''} — staying quiet", flush=True)
            ui.emit("info", text="Ignored background voices")
            if outcome is not None:
                outcome["ignored"] = "model"
            return False
        took = time.monotonic() - started
        spoken = _show_and_say(store, speaker, ui, said, answer, took)
        current = answer
        corrections = 0
        while spoken is not None and getattr(spoken, "audio", None) and corrections < 5:
            corrections += 1
            store.log("voice", "interrupted", f"after: {spoken.heard_text[:120]}")
            if not _has_speech(spoken.audio):  # a noise, not someone talking: carry on
                spoken = _resume(store, speaker, current, spoken.heard_text)
                continue
            print("✋ Adjusting to what you said…", flush=True)  # they talked over it: take the correction
            ui.emit("state", state="thinking")
            started = time.monotonic()
            try:
                said, answer = quick.handle(audio=spoken.audio, interrupted=spoken.heard_text)
            except QuickError as exc:
                print(f"❌ {exc}")
                ui.emit("error", text=str(exc))
                break
            if not answer:  # it wasn't meant for Jarvis: finish what it was saying
                spoken = _resume(store, speaker, current, spoken.heard_text)
                continue
            current = answer
            spoken = _show_and_say(store, speaker, ui, said, answer, time.monotonic() - started)
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


def _show_and_say(store: Store, speaker, ui, said: str, answer: str, took: float):
    if said:
        print(f"👂 {said}")
        store.log("ears", "understood", said)
        ui.emit("heard", text=said)
    store.log("brain", "reply", answer)
    print(f"⚡ {answer}", flush=True)
    ui.emit("answer", text=answer, seconds=took)
    spoken = speaker.say(answer, interruptible=True)
    voice_s = getattr(speaker, "first_audio_s", None)
    print(f"   ⏱ understood + acted in {took:.1f}s" + (f" · voice started {voice_s:.1f}s later" if voice_s else ""))
    return spoken


def _has_speech(audio: bytes) -> bool:
    from .nearby import speech_levels

    try:
        return speech_levels(wav_samples(audio))["voiced_s"] >= 0.4
    except Exception:  # noqa: BLE001 — can't measure: assume it was speech
        return True


def _resume(store: Store, speaker, answer: str, heard: str):
    """A false interruption: say the rest of the reply instead of going quiet."""
    cut = heard.rstrip("…")
    rest = answer[len(cut):].strip() if answer.startswith(cut) else answer
    print("↩️  Not a real interruption — carrying on", flush=True)
    store.log("voice", "false_interrupt", "resumed the reply")
    return speaker.say(rest, interruptible=True) if rest else None


_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|send|send it|go ahead|do it|confirm|correct|right|please do)\b|"
                  r"(^|\s)(اه|آه|ايوه|أيوه|ايوة|أيوة|تمام|ماشي|ابعت|ابعتها|ابعته|يلا|أكيد|اكيد|نعم|موافق)($|\s)", re.I)
_NO = re.compile(r"\b(no|nope|cancel|stop|don'?t|wait|hold on|never mind)\b|"
                 r"(^|\s)(لا|لأ|لاء|استنى|الغي|إلغي|بلاش|متبعتش|وقف)($|\s)", re.I)


def yes_or_no(*texts: str) -> bool | None:
    """A spoken answer to "shall I?": True, False, or None when it's unclear. "No" wins ties."""
    said = " ".join(t for t in texts if t)
    if _NO.search(said):
        return False
    if _YES.search(said):
        return True
    return None


def approval_question(action: str, args: dict) -> str:
    """What Jarvis says when it needs a yes: the exact thing it's about to do."""
    if action == "whatsapp_send":
        return f"Send this to {args.get('contact', 'them')} on WhatsApp: {args.get('message', '')}. Shall I send it?"
    if action == "delete_file":
        return f"Delete {args.get('path', 'that file')}? It goes to the Jarvis trash. Yes or no?"
    if action == "close_app":
        return f"Close {args.get('name', 'that app')}? Unsaved work could be lost. Yes or no?"
    return f"I need your OK to {action.replace('_', ' ')}. Yes or no?"


def _pick_voice(raw: voice.Speaker, choice: str, profile_voice: str) -> None:
    """--voice auto (default): the profile's voice decides the engine (Ava → Edge, Aoede → Gemini)."""
    if choice in ("off", "windows"):
        raw.voice = choice
        return
    engine = voice.engine_for(profile_voice)[0]
    if choice == "auto" or choice == engine:
        raw.set_voice(profile_voice)
    else:
        raw.set_voice(voice.DEFAULT_VOICE if choice == "edge" else "Aoede")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.assistant", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--push-to-talk", action="store_true", help="press Enter instead of saying Hey Jarvis")
    parser.add_argument("--to", choices=["quick", "claude", "print"], default="quick",
                        help="quick: Gemini does light tasks, hands heavy ones to Claude (default)")
    parser.add_argument("--voice", choices=["auto", "edge", "gemini", "windows", "off"], default=voice.default_voice(),
                        help="auto (default): your profile's voice (Ava = Microsoft neural, no daily limit)")
    parser.add_argument("--barge-in", choices=["auto", "talk", "wake", "off"],
                        default=os.environ.get("JARVIS_BARGE_IN", "auto"),
                        help="how to interrupt Jarvis: auto = by talking with headphones, by 'Hey Jarvis' on "
                             "speakers; talk = always by talking; wake = only 'Hey Jarvis'; off. "
                             "Esc, a tap or typing always work")
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
    out_levels: collections.deque = collections.deque(maxlen=40)

    def on_voice_level(v: float) -> None:
        out_levels.append((time.monotonic(), v * 6000))  # what we play: lets barge-in ignore our own echo
        ui.emit("level", value=v, source="voice")

    def out_level() -> float:
        now = time.monotonic()
        return max((lv for t, lv in list(out_levels) if now - t < 1.0), default=0.0)  # covers speaker delay

    raw_speaker = voice.Speaker("edge" if voice._edge_available() else "gemini", on_level=on_voice_level,
                                cache_dir=settings.home / "voice_cache",
                                log=lambda kind, summary: store.log("voice", kind, summary))
    _pick_voice(raw_speaker, args.voice, profile.voice)
    voice_name = {"edge": raw_speaker.edge_voice, "gemini": raw_speaker.gemini_voice}.get(raw_speaker.voice,
                                                                                         raw_speaker.voice)
    print(f"🔊 Voice: {voice_name} ({raw_speaker.voice})", flush=True)

    voiceprint = VoicePrint(settings.home)
    nearby = NearbyGate(store)

    def locked() -> bool:
        return voiceprint.enrolled and Profile.load(settings.home).voice_lock

    if locked():  # load the voice model now, not on the first request
        threading.Thread(target=voiceprint.embedder, daemon=True).start()
        print("🔒 Voice lock ON: only your enrolled voice is answered (say 'listen to everyone' to turn off)")
    if profile.nearby_only:
        print("📏 Answering voices close to the laptop; distant ones are ignored "
              "(say 'answer voices from anywhere' to turn off)", flush=True)
    talk_to_interrupt = args.barge_in == "talk" or (args.barge_in == "auto" and voice.headphones_in_use())
    print("✋ Interrupt me: " + ("just talk, " if talk_to_interrupt else "") +
          "say 'Hey Jarvis', press Esc, tap the orb or type.", flush=True)

    wake_ref: dict = {"wake": None}
    outcome: dict = {"ignored": ""}  # "voice"/"far" = checked locally, "model" = not meant for Jarvis

    def make_barge(text: str):
        if args.barge_in == "off":
            return None
        wake = wake_ref["wake"] if "jarvis" not in text.lower() else None  # don't trigger on our own words
        energy = talk_to_interrupt and raw_speaker.levels_available
        if not energy and wake is None:
            return None
        return voice.BargeIn(out_level=out_level, voiceprint=voiceprint if locked() else None, wake=wake,
                             energy=energy)

    speaker = _UISpeaker(raw_speaker, ui, barge=make_barge)
    ui.emit("profile", assistant_name=profile.assistant_name, user_name=profile.user_name)
    stop = threading.Event()
    control = Control()

    quick = None
    if args.to == "quick":
        spoken_answer: dict = {"token": None, "value": None}

        def listen_for_answer(token: str) -> None:
            """Say "yes / send it / تمام" or "no / لا" instead of pressing a key."""
            try:
                audio = voice.record_until_silence(max_s=8, no_speech_s=max(5.0, settings.approval_wait_s - 3))
                if audio is None or spoken_answer["token"] != token:
                    return
                heard = hear(settings, store, audio=audio)
                if heard is not None and spoken_answer["token"] == token:
                    spoken_answer["value"] = yes_or_no(heard.original, heard.english)
                    print(f"   🗣  {heard.original} → {spoken_answer['value']}", flush=True)
            except Exception as exc:  # noqa: BLE001 — keys and the app still work
                print(f"   (couldn't listen for a spoken yes/no: {exc})")

        def announce(what: str) -> None:
            print(f"\n🔐 Approval needed: {what}\n   Say yes or no, press Y / N, or use the app.", flush=True)
            pending = store.pending_approvals()
            token = None
            if pending:
                p = pending[-1]
                token = p["id"]
                ui.emit("approval", id=p["id"], action=p["action"], args=p["args"], seconds=settings.approval_wait_s)
                speaker.say(approval_question(p["action"], p["args"]))
            else:
                speaker.say("I need your approval. Yes or no?")
            spoken_answer.update(token=token, value=None)
            threading.Thread(target=listen_for_answer, args=(token,), daemon=True).start()

        def approval_key() -> bool | None:
            key = voice.yes_no_pressed()
            if key is not None:
                spoken_answer["token"] = None  # decided: ignore a late spoken answer
                return key
            if spoken_answer["value"] is not None:
                value, spoken_answer["value"], spoken_answer["token"] = spoken_answer["value"], None, None
                return value
            return None

        def on_profile(p: Profile) -> None:  # "call me…", "your name is…", "use a different voice"
            if args.voice not in ("off", "windows"):
                raw_speaker.set_voice(p.voice)
            ui.emit("profile", assistant_name=p.assistant_name, user_name=p.user_name)

        gateway = Gateway(settings, store, on_approval_needed=announce, approval_key=approval_key,
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

        filler = {"said": False}

        def on_event(kind: str, **data) -> None:
            ui.emit(kind, **data)
            if kind == "turn":
                filler["said"] = False
            elif kind == "tool" and data.get("name") in SLOW_TOOLS and not filler["said"]:
                filler["said"] = True  # say something while it works, instead of silence
                lang = "arabic" if Profile.load(settings.home).reply_language == "arabic" else "english"
                threading.Thread(target=speaker.say, args=(random.choice(FILLERS[lang]),),
                                 kwargs={"after": "thinking", "from_other_thread": True}, daemon=True).start()

        quick = QuickBrain(settings, gateway, ask_claude=claude, on_event=on_event)

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
        wake_ref["wake"] = wake

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

        wait = args.follow_up
        last = store.recent_exchanges(1)
        if follow_up and last and last[0]["answer"].rstrip().endswith(("?", "؟")):
            wait = max(wait, 15.0)  # it asked you something: give you time to answer
        audio = voice.record_until_silence(on_start=ready, no_speech_s=wait if follow_up else 8.0,
                                           on_level=lambda v: ui.emit("level", value=v))
        if audio is not None:
            voice.chime("stop")
            samples = wav_samples(audio)
            if Profile.load(settings.home).nearby_only:
                close, info = nearby.check(samples, lenient=not follow_up)  # just said "Hey Jarvis": lenient
                if not close:
                    print(f"🔇 Ignored a distant voice — {info['why']}", flush=True)
                    ui.emit("info", text="Ignored a distant voice")
                    store.log("voice", "ignored_far", info["why"])
                    outcome["ignored"] = "far"
                    return None
            if locked() and not voiceprint.matches(samples):
                score = voiceprint.last_score
                print(f"🙉 Ignored a voice that isn't yours (match {score:.2f})" if score is not None
                      else "🙉 Ignored: couldn't tell whose voice that was", flush=True)
                ui.emit("info", text="Ignored a voice that isn't yours")
                store.log("voice", "ignored_not_you", f"match {voiceprint.last_score}")
                outcome["ignored"] = "voice"
                return None
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
            strangers = 0
            while not stop.is_set():  # conversation: no wake word until the user goes quiet
                typed = command[1] if command and command[0] == "text" else None
                command = None
                outcome["ignored"] = ""
                try:
                    spoke = handle_one(settings, store, speaker, lambda: record(follow_up), to=args.to,
                                       new_session=first, quick=quick, ui=ui, text=typed, outcome=outcome)
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
                if spoke and typed is None:
                    nearby.learn()  # a real, answered request: this is what "nearby" sounds like
                if not spoke and outcome["ignored"] in ("voice", "far") and strangers < 3:
                    strangers += 1  # someone else talked (checked on this laptop, free): keep listening for you
                    follow_up = True
                    continue
                if not spoke:
                    if not follow_up and not outcome["ignored"]:
                        speaker.say("I didn't hear anything.")
                    break
                strangers = 0
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

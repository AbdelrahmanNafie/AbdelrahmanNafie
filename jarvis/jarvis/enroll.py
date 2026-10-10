"""Teach Jarvis your voice, so it ignores other people, the TV and its own echo.

  python -m jarvis.enroll            # read 6 short sentences (about a minute)
  python -m jarvis.enroll --test     # say something: shows how well it matches you
  python -m jarvis.enroll --forget   # delete the voiceprint (Jarvis listens to everyone again)

Everything runs on this laptop. The voiceprint is 256 numbers in JARVIS_HOME/voiceprint.npy:
it can tell whether a voice is yours, but it can't be turned back into your voice.
"""

from __future__ import annotations

import argparse

from . import config, voice
from .speaker_id import VoicePrint, wav_samples

SENTENCES = [
    "Hey Jarvis, open my project folder and check what's new today.",
    "Remind me in twenty minutes to call the client about the invoice.",
    "What's the weather like in Cairo this weekend?",
    "افتح الكروم ودوّرلي على آخر أخبار الذكاء الاصطناعي",
    "Read me the last email and draft a short, friendly reply.",
    "فكرني بكرة الساعة عشرة الصبح عندي اجتماع مهم",
]


def _record(prompt: str) -> bytes | None:
    print(f"\n   🗣  “{prompt}”")
    return voice.record_until_silence(max_s=12, no_speech_s=8,
                                      on_start=lambda: (print("   (speak now)", flush=True), voice.chime("start")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.enroll", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="check how well your voice matches")
    parser.add_argument("--forget", action="store_true", help="delete the voiceprint")
    args = parser.parse_args(argv)
    settings = config.load()
    vp = VoicePrint(settings.home)

    if args.forget:
        vp.forget()
        print("Voiceprint deleted. Jarvis listens to everyone again.")
        return 0

    if args.test:
        if not vp.enrolled:
            print("No voiceprint yet. Run: python -m jarvis.enroll")
            return 1
        audio = _record("Say anything for a few seconds")
        if audio is None:
            print("I didn't hear anything.")
            return 1
        ok = vp.matches(wav_samples(audio))
        score = vp.last_score
        print(f"\n   Match: {score:.2f} (needs {vp.threshold:.2f}) → {'✅ that is you' if ok else '❌ not you'}"
              if score is not None else "\n   Too little speech to tell.")
        print("   Ask someone else to try: they should get ❌. Adjust with JARVIS_VOICE_MATCH (e.g. 0.40).")
        return 0

    print("Let's learn your voice. Read each sentence normally, in your usual spot, at your usual volume.")
    print("Use the microphone you'll use with Jarvis. Takes about a minute.")
    clips = []
    for sentence in SENTENCES:
        audio = _record(sentence)
        if audio is not None:
            clips.append(wav_samples(audio))
    try:
        result = vp.enroll(clips)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    print(f"\n✅ Saved your voiceprint from {result['clips']} recordings "
          f"(consistency {result['self_match_min']:.2f}; accepts ≥ {result['threshold']:.2f}).")
    if result["self_match_min"] < result["threshold"] + 0.1:
        print("   Your recordings varied a lot (noise?). If Jarvis ignores you, run this again somewhere quieter.")
    print("   Test it: python -m jarvis.enroll --test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

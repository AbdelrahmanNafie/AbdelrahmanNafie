"""Who Jarvis is and who you are: names, voice, reply language. Saved in JARVIS_HOME/profile.json.

Changed by voice ("call me Abdelrahman", "your name is Nova", "use a different voice")
through the set_preference tool, and read fresh on every turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Female voices. Edge (Microsoft neural, no daily limit): Ava, Emma, Jenny, Aria, Sonia, Salma
# (Egyptian Arabic). Gemini (very natural, small daily quota): Aoede, Kore, Leda, Zephyr…
FEMALE_VOICES = ["Ava", "Emma", "Jenny", "Salma", "Aoede", "Kore", "Leda", "Zephyr"]
REPLY_LANGUAGES = {"english", "arabic", "same"}  # "same" = answer in the language you spoke


@dataclass
class Profile:
    user_name: str = ""
    assistant_name: str = "Jarvis"
    voice: str = "Ava"  # Edge voice (Ava, Salma…), Gemini voice (Aoede, Kore…) or "windows"
    reply_language: str = "english"
    proactive: bool = True
    activity_tracking: bool = True  # note the app/window in front (local), so Jarvis learns how you work
    voice_lock: bool = False  # with an enrolled voiceprint: ignore voices that aren't yours (opt-in)
    nearby_only: bool = True  # answer anyone close to the laptop, ignore distant voices
    confirm_sends: bool = True  # ask "send it?" before sending a message as the user
    version: int = 3

    @classmethod
    def load(cls, home: Path) -> "Profile":
        path = home / "profile.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        if data.get("version", 1) < 2 and data.get("voice") == "Aoede":
            data["voice"] = "Ava"  # old default: Gemini's voice runs out of quota and then changes
        if data.get("version", 1) < 3:
            data["voice_lock"] = False  # it rejected the user's own voice: now opt-in ("listen only to me")
        data["version"] = 3
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, home: Path) -> None:
        (home / "profile.json").write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")


class PreferenceError(ValueError):
    pass


def apply(profile: Profile, key: str, value: str) -> Profile:
    """Validate and apply one preference change (raises PreferenceError with a friendly message)."""
    key = key.strip().lower().replace(" ", "_")
    value = str(value).strip()
    if key in ("user_name", "assistant_name"):
        if not 0 < len(value) <= 40:
            raise PreferenceError("names must be 1-40 characters")
        setattr(profile, key, value)
    elif key == "voice":
        from .voice import EDGE_VOICES, GEMINI_VOICES

        low = value.lower()
        if low == "windows":
            profile.voice = "windows"
        elif low in EDGE_VOICES or low in {v.lower() for v in GEMINI_VOICES}:
            profile.voice = value[:1].upper() + value[1:].lower()
        elif re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}-\w+Neural", value):
            profile.voice = value
        else:
            raise PreferenceError(f"voice must be one of {', '.join(FEMALE_VOICES)} (or another Gemini/Edge "
                                  "voice name), or 'windows'")
    elif key == "reply_language":
        if value.lower() not in REPLY_LANGUAGES:
            raise PreferenceError(f"reply_language must be one of {sorted(REPLY_LANGUAGES)}")
        profile.reply_language = value.lower()
    elif key in ("proactive", "activity_tracking", "voice_lock", "nearby_only", "confirm_sends"):
        setattr(profile, key, value.lower() in ("on", "true", "yes", "1"))
    else:
        raise PreferenceError("you can change: user_name, assistant_name, voice, reply_language, proactive, "
                              "activity_tracking, voice_lock, nearby_only, confirm_sends")
    return profile

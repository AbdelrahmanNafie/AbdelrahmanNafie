"""Who Jarvis is and who you are: names, voice, reply language. Saved in JARVIS_HOME/profile.json.

Changed by voice ("call me Abdelrahman", "your name is Nova", "use a different voice")
through the set_preference tool, and read fresh on every turn.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# Gemini prebuilt voices that sound female (our pick; Google's docs describe them as
# Bright / Firm / Youthful / Breezy / Easy-going…). Any Gemini voice name works.
FEMALE_VOICES = ["Aoede", "Kore", "Leda", "Zephyr", "Callirrhoe", "Despina", "Sulafat", "Achernar"]
REPLY_LANGUAGES = {"english", "arabic", "same"}  # "same" = answer in the language you spoke


@dataclass
class Profile:
    user_name: str = ""
    assistant_name: str = "Jarvis"
    voice: str = "Aoede"  # a Gemini voice name, or "windows" for the built-in Windows voice
    reply_language: str = "english"
    proactive: bool = True

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
        if value.lower() == "windows":
            profile.voice = "windows"
        elif value.isalpha() and len(value) <= 20:
            profile.voice = value[:1].upper() + value[1:].lower()
        else:
            raise PreferenceError(f"voice must be a Gemini voice name (e.g. {', '.join(FEMALE_VOICES[:4])}) "
                                  "or 'windows'")
    elif key == "reply_language":
        if value.lower() not in REPLY_LANGUAGES:
            raise PreferenceError(f"reply_language must be one of {sorted(REPLY_LANGUAGES)}")
        profile.reply_language = value.lower()
    elif key == "proactive":
        profile.proactive = value.lower() in ("on", "true", "yes", "1")
    else:
        raise PreferenceError("you can change: user_name, assistant_name, voice, reply_language, proactive")
    return profile

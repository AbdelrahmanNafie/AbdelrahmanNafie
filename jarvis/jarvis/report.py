"""A readable report of what Jarvis heard, did and said — to find out why something went wrong.

  python -m jarvis.report              # last 24 hours → JARVIS_HOME/report.md
  python -m jarvis.report --hours 72

The report contains your conversation with Jarvis, so read it before sharing it with anyone.
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from pathlib import Path

from . import config
from .profile import Profile
from .store import Store

# Events that explain problems; everything else is summarized in the counts.
_SHOW = {("ears", "understood"), ("brain", "reply"), ("brain", "proposed"), ("hands", "failed"),
         ("hands", "executed"), ("policy", "deny"), ("policy", "needs_approval"), ("human", "expired"),
         ("human", "rejected"), ("human", "approved"), ("brain", "failed"), ("ears", "failed")}


def build(store: Store, home: Path, hours: float = 24.0, now: float | None = None) -> str:
    now = time.time() if now is None else now
    since = now - hours * 3600
    events = store.events_since(since)
    exchanges = store.exchanges_since(since)
    counts = Counter(f"{e['stage']}/{e['kind']}" for e in events)
    profile = Profile.load(home)
    rules = [m for m in store.memories(limit=500) if m["category"] == "instructions"]
    stamp = lambda ts: time.strftime("%a %H:%M:%S", time.localtime(ts))  # noqa: E731

    lines = [f"# Jarvis report — last {hours:g} hours", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Contains your conversation: review before sharing.", "",
             "## Summary", "",
             f"- Requests answered: {len(exchanges)}",
             f"- Actions done: {counts['hands/executed']} · failed: {counts['hands/failed']} · "
             f"blocked by policy: {counts['policy/deny']} · approvals expired/rejected: "
             f"{counts['human/expired'] + counts['human/rejected']}",
             f"- Voice problems: {counts['voice/voice_failed']} · interruptions: {counts['voice/interrupted']} "
             f"(false alarms resumed: {counts['voice/false_interrupt']})",
             f"- Ignored: distant voices {counts['voice/ignored_far']} · not-your-voice "
             f"{counts['voice/ignored_not_you']}",
             f"- Errors: Gemini/Claude {counts['brain/failed'] + counts['ears/failed']}", "",
             "## Settings", "",
             f"- Names: you = {profile.user_name or '(not set)'}, assistant = {profile.assistant_name}",
             f"- Voice: {profile.voice} · reply language: {profile.reply_language} · nearby_only: "
             f"{profile.nearby_only} · voice_lock: {profile.voice_lock}",
             f"- Env: JARVIS_VOICE={os.environ.get('JARVIS_VOICE', 'auto')}, "
             f"JARVIS_BARGE_IN={os.environ.get('JARVIS_BARGE_IN', 'auto')}, "
             f"JARVIS_THINKING={os.environ.get('JARVIS_THINKING', 'low')}", "",
             f"## Standing instructions ({len(rules)})", ""]
    lines += [f"- #{m['id']} {m['text']}" for m in sorted(rules, key=lambda m: m["id"])] or ["- (none)"]
    lines += ["", "## Conversation", ""]
    for ex in exchanges:
        lines += [f"**{stamp(ex['ts'])} — you:** {ex['said'] or '(unclear)'}  ", f"**Jarvis:** {ex['answer']}", ""]
    lines += ["## Timeline (actions, failures, voice events)", ""]
    for e in events:
        if (e["stage"], e["kind"]) in _SHOW or e["stage"] == "voice":
            lines.append(f"- {stamp(e['ts'])} `{e['stage']}/{e['kind']}` {e['summary'][:300]}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.report", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = config.load()
    out = args.out or settings.home / "report.md"
    out.write_text(build(Store(settings.db_path), settings.home, args.hours), "utf-8")
    print(f"Report written to {out}\nRead it first (it contains your conversation), then share it if you like.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

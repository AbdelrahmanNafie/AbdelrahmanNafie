"""Deterministic policy: decides what may run. No AI involved here.

The brain (Claude) can only *propose* actions by calling tools. Every proposal
lands here first. Unknown actions are denied, dangerous ones wait for a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Risk(StrEnum):
    READ = "read"  # no side effects -> run immediately
    CHANGE = "change"  # reversible side effects -> run and log
    DANGER = "danger"  # destructive / outward-facing -> human must approve


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


# The complete list of things the hands can do. Adding a capability means
# adding a line here *and* an implementation in actions.py — nothing else runs.
ACTION_RISK: dict[str, Risk] = {
    "list_files": Risk.READ,
    "read_file": Risk.READ,
    "system_info": Risk.READ,
    "write_note": Risk.CHANGE,
    "open_app": Risk.CHANGE,
    "delete_file": Risk.DANGER,
    "open_url": Risk.CHANGE,
    "web_search": Risk.CHANGE,
    "fetch_page": Risk.READ,
    "search_files": Risk.READ,
    "open_path": Risk.CHANGE,
    "draft_message": Risk.CHANGE,  # opens a draft; the user presses Send
    "code_task": Risk.DANGER,  # Claude edits project files
    "media_key": Risk.CHANGE,
    "lock_screen": Risk.CHANGE,
    "list_running_apps": Risk.READ,
    "close_app": Risk.DANGER,  # could lose unsaved work
    "clipboard_read": Risk.READ,
    "clipboard_write": Risk.CHANGE,
    "set_reminder": Risk.CHANGE,
    "remember": Risk.CHANGE,
    "recall": Risk.READ,
    "forget": Risk.CHANGE,  # soft delete
    "db_add": Risk.CHANGE,
    "db_find": Risk.READ,
    "db_update": Risk.CHANGE,
    "db_delete": Risk.CHANGE,  # soft delete
    "set_preference": Risk.CHANGE,
    "go_to_sleep": Risk.CHANGE,
    "restart_jarvis": Risk.CHANGE,
    "web_answer": Risk.READ,
    "improve_myself": Risk.DANGER,  # edits Jarvis's own code (tested, auto-undone on failure)
}


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    risk: Risk | None
    reason: str


def decide(action: str, *, paused: bool) -> Decision:
    risk = ACTION_RISK.get(action)
    if risk is None:
        return Decision(Verdict.DENY, None, f"'{action}' is not an allowlisted action")
    if paused and risk is not Risk.READ:
        return Decision(Verdict.DENY, risk, "Jarvis is paused (kill switch on); only read actions run")
    if risk is Risk.DANGER:
        return Decision(Verdict.NEEDS_APPROVAL, risk, "destructive action requires human approval")
    return Decision(Verdict.ALLOW, risk, f"{risk.value} action is pre-approved")

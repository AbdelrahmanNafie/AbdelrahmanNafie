"""Multi-step skills on top of the desktop hands, each step checked on screen before the next.

whatsapp_message works ONLY inside the user's own WhatsApp Web tab, already open and logged in in
their browser: switch to that tab → search the chat → check the right chat is open → type →
(if asked) send → check it was sent. Boundaries:
- it never opens WhatsApp itself, never uses another account, number, app or API;
- it never types into a chat it couldn't confirm on screen;
- sending needs the user's "yes" unless they turned confirmation off ("send without asking").
"""

from __future__ import annotations

import difflib
import time
import unicodedata
from typing import Any, Callable

from .desktop import DesktopError
from .vision import valid_point

_STATE = """This should be WhatsApp Web in a browser tab.
Return {{"is_whatsapp": true/false, "logged_in": true/false (false if a QR code / "link a device" screen
is showing), "open_chat": "<name shown in the header of the currently open conversation, or null>",
"search_box": [y, x] of the "Search" / "Search or start a new chat" field above the chat list (or null),
"message_box": [y, x] of the "Type a message" field at the bottom of the open conversation (or null)}}.
The chat we want is: "{contact}"."""

_RESULTS = """The chat list / search results of WhatsApp are on the left. List the chats or contacts
currently shown there: {{"results": [{{"name": "<chat name exactly as shown>", "point": [y, x] of that row}}]}}
(at most 12, top to bottom). We are looking for "{contact}"."""

_SENT = """In the open WhatsApp conversation, is the LAST message on the right side (sent by the user)
this text (allow small differences): "{text}"? Return {{"sent": true/false, "still_in_box": true/false
(true if that text is still sitting in the "Type a message" field)}}."""


def norm(name: str | None) -> str:
    """Comparable form of a name: case, accents, emoji and punctuation removed."""
    text = unicodedata.normalize("NFKD", name or "").lower()
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    return " ".join(text.split())


def same_chat(shown: str | None, wanted: str) -> bool:
    a, b = norm(shown), norm(wanted)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b in a.split() or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def best_match(results: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    rows = [r for r in results or [] if isinstance(r, dict) and r.get("name") and valid_point(r.get("point"))]
    w = norm(wanted)
    for test in (lambda n: n == w, lambda n: n.startswith(w), lambda n: w in n.split(), lambda n: w in n):
        hits = [r for r in rows if test(norm(r["name"]))]
        if hits:
            return hits[0]
    scored = sorted(rows, key=lambda r: -difflib.SequenceMatcher(None, norm(r["name"]), w).ratio())
    if scored and difflib.SequenceMatcher(None, norm(scored[0]["name"]), w).ratio() >= 0.75:
        return scored[0]
    return None


def find_whatsapp(desk) -> dict[str, Any]:
    """The user's WhatsApp Web tab (any open browser window, active tab or not). Nothing else."""
    for t in desk.browser_tabs():
        if "whatsapp" in t["tab"].lower():
            return {"kind": "tab", "hwnd": t["hwnd"], "tab": t["tab"], "label": f"your WhatsApp tab in {t['app']}"}
    raise DesktopError("I can't find a WhatsApp Web tab in your browser. Open web.whatsapp.com in your browser "
                       "(logged in to your account), then ask me again")


def whatsapp_message(desk, see, contact: str, message: str, *, send: bool,
                     sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    contact, message = contact.strip(), message.strip()
    if not contact or not message:
        raise DesktopError("I need both the chat name and the message")
    steps: list[str] = []
    target = find_whatsapp(desk)
    steps.append(f"switched to {target['label']}")
    desk.activate(target)
    sleep(0.6)

    shot = desk.screenshot()
    state = see(shot.jpeg, _STATE.format(contact=contact))
    if state.get("is_whatsapp") is False:
        raise DesktopError(f"{target['label']} doesn't look like WhatsApp")
    if state.get("logged_in") is False:
        raise DesktopError("WhatsApp Web isn't linked yet (QR code showing): scan it with your phone first")

    if same_chat(state.get("open_chat"), contact):
        steps.append(f"the chat with {state['open_chat']} was already open")
    else:
        desk.check_safe()
        if valid_point(state.get("search_box")):
            desk.click(*shot.to_screen(state["search_box"]))
        else:
            desk.keys("ctrl+alt+/")  # WhatsApp Web's search shortcut
        desk.keys("ctrl+a")
        desk.type(contact)
        sleep(1.6)
        shot = desk.screenshot()
        results = see(shot.jpeg, _RESULTS.format(contact=contact)).get("results") or []
        pick = best_match(results, contact)
        if pick is None:
            names = [r.get("name") for r in results if isinstance(r, dict) and r.get("name")][:6]
            desk.keys("esc")
            raise DesktopError(f"no chat called '{contact}' in WhatsApp"
                               + (f" — I can see: {', '.join(names)}" if names else ""))
        desk.click(*shot.to_screen(pick["point"]))
        sleep(1.0)
        shot = desk.screenshot()
        state = see(shot.jpeg, _STATE.format(contact=contact))
        if not same_chat(state.get("open_chat"), pick["name"]):
            raise DesktopError(f"I clicked '{pick['name']}' but couldn't confirm that chat opened, so I typed nothing")
        steps.append(f"opened the chat with {state['open_chat']}")

    if not valid_point(state.get("message_box")):
        raise DesktopError("I can't find the message box in that chat")
    desk.check_safe()
    desk.click(*shot.to_screen(state["message_box"]))
    desk.type(message)
    steps.append("typed the message")
    result = {"chat": state.get("open_chat"), "steps": steps, "sent": False}
    if not send:
        result["note"] = "Typed but NOT sent: the user can press Enter, or ask you to send it."
        return result

    desk.keys("enter")
    sleep(1.2)
    check = see(desk.screenshot().jpeg, _SENT.format(text=message[:120]))
    result["sent"] = bool(check.get("sent")) or check.get("still_in_box") is False
    result["confirmed_on_screen"] = bool(check.get("sent"))
    steps.append("pressed Enter" + (" — the message shows as sent" if check.get("sent") else ""))
    if check.get("still_in_box"):
        result["note"] = "The text still seems to be in the box: Enter may not have sent it."
    return result


def click_on(desk, see, target: str) -> dict[str, Any]:
    """Click something described in words ("the Send button", "Soly's chat") in the window in front."""
    desk.check_safe()
    shot = desk.screenshot()
    found = see(shot.jpeg, f'Find: "{target}". Return {{"found": true/false, "point": [y, x] of its centre, '
                           f'"label": "<the text on it, if any>"}}.')
    if not found.get("found") or not valid_point(found.get("point")):
        raise DesktopError(f"I can't see '{target}' in the window in front")
    desk.click(*shot.to_screen(found["point"]))
    return {"clicked": found.get("label") or target}

"""Operating apps on screen, and the WhatsApp task that failed in real use:
"go to my WhatsApp tab, open Soli's chat, type the message and send it"."""

import pytest

from jarvis import assistant, desktop, profile, skills, toolset
from jarvis.desktop import DesktopError, Shot
from jarvis.gateway import Gateway


class FakeDesktop:
    """Records what Jarvis does with the mouse and keyboard."""

    def __init__(self, tabs=None, windows=None, front=("chrome", "Gmail - Google Chrome")):
        self.tabs = tabs if tabs is not None else [
            {"hwnd": 1, "app": "chrome", "window": "Gmail - Google Chrome", "tab": "Gmail"},
            {"hwnd": 1, "app": "chrome", "window": "Gmail - Google Chrome", "tab": "(2) WhatsApp"},
        ]
        self.wins = windows or [{"hwnd": 1, "app": "chrome", "title": "Gmail - Google Chrome"}]
        self.front = front
        self.log = []

    def browser_tabs(self):
        return self.tabs

    def windows(self):
        return self.wins

    def find(self, name):
        return desktop.Desktop.find(self, name)

    def activate(self, target):
        self.log.append(("activate", target.get("tab") or target["label"]))

    def foreground(self):
        return 1, *self.front

    def check_safe(self):
        desktop.Desktop.check_safe(self)

    def screenshot(self):
        return Shot(b"jpeg", 0, 0, 1000, 1000)  # 0-1000 maps 1:1 to pixels

    def click(self, x, y, double=False):
        self.log.append(("click", x, y))

    def keys(self, combo):
        desktop.parse_keys(combo)  # same validation as the real one
        self.log.append(("keys", combo))

    def type(self, text):
        self.log.append(("type", text))

    def open_url(self, url):
        self.log.append(("open", url))


def screens(*answers):
    """A fake Gemini vision that answers screen questions in order."""
    asked = []
    queue = list(answers)

    def see(jpeg, task):
        asked.append(task)
        return queue.pop(0)
    see.asked = asked
    return see


WHATSAPP = {"is_whatsapp": True, "logged_in": True, "open_chat": "Mom", "search_box": [100, 200],
            "message_box": [950, 600]}
SOLI_OPEN = {**WHATSAPP, "open_chat": "Soli"}
RESULTS = {"results": [{"name": "Solaf Work", "point": [250, 150]}, {"name": "Soli", "point": [320, 150]}]}


def test_switches_to_the_whatsapp_tab_finds_soli_types_and_sends():
    desk = FakeDesktop()
    see = screens(WHATSAPP, RESULTS, SOLI_OPEN, {"sent": True, "still_in_box": False})
    r = skills.whatsapp_message(desk, see, "Soly", "I'll be there at 8", send=True, sleep=lambda s: None)
    assert desk.log == [
        ("activate", "(2) WhatsApp"),          # the background tab, not the Gmail tab in front
        ("click", 200, 100),                    # search box
        ("keys", "ctrl+a"), ("type", "Soly"),   # search for the chat
        ("click", 150, 320),                    # "Soli" — close enough to "Soly", not "Solaf Work"
        ("click", 600, 950),                    # message box, only after the header said "Soli"
        ("type", "I'll be there at 8"),
        ("keys", "enter"),
    ]
    assert r["sent"] and r["confirmed_on_screen"] and r["chat"] == "Soli"


def test_typing_only_does_not_press_enter():
    desk = FakeDesktop()
    r = skills.whatsapp_message(desk, screens(SOLI_OPEN), "Soli", "hi", send=False, sleep=lambda s: None)
    assert ("keys", "enter") not in desk.log and not r["sent"] and "NOT sent" in r["note"]


def test_only_the_users_own_open_whatsapp_tab_is_used():
    app_only = FakeDesktop(tabs=[{"hwnd": 1, "app": "chrome", "window": "x", "tab": "Gmail"}],
                           windows=[{"hwnd": 9, "app": "whatsapp", "title": "WhatsApp"}])
    with pytest.raises(DesktopError, match="can't find a WhatsApp Web tab"):
        skills.whatsapp_message(app_only, screens(), "Soli", "hi", send=True, sleep=lambda s: None)
    assert app_only.log == []  # opened nothing, used no other app


def test_never_types_into_a_chat_it_could_not_confirm():
    desk = FakeDesktop()
    see = screens(WHATSAPP, RESULTS, {**WHATSAPP, "open_chat": "Mom"})
    with pytest.raises(DesktopError, match="couldn't confirm"):
        skills.whatsapp_message(desk, see, "Soli", "secret", send=True, sleep=lambda s: None)
    assert ("type", "secret") not in desk.log and ("keys", "enter") not in desk.log


def test_unknown_contact_says_what_it_can_see():
    desk = FakeDesktop()
    see = screens(WHATSAPP, {"results": [{"name": "Sara", "point": [300, 100]}, {"name": "Samir", "point": [350, 100]}]})
    with pytest.raises(DesktopError, match="I can see: Sara, Samir"):
        skills.whatsapp_message(desk, see, "Soli", "hi", send=False, sleep=lambda s: None)
    assert desk.log[-1] == ("keys", "esc")


def test_logged_out_whatsapp_is_reported():
    with pytest.raises(DesktopError, match="QR code"):
        skills.whatsapp_message(FakeDesktop(), screens({**WHATSAPP, "logged_in": False}), "Soli", "hi",
                                send=False, sleep=lambda s: None)


def test_names_match_like_a_person_would():
    assert skills.same_chat("Soli 🌸", "soli") and skills.same_chat("Soli Ahmed", "Soli")
    assert skills.same_chat("سولي", "سولي") and not skills.same_chat("Mom", "Soli")
    assert skills.best_match(RESULTS["results"], "soly")["name"] == "Soli"


# ---------------------------------------------------------------- boundaries
def test_sending_needs_a_yes_unless_turned_off(settings, store):
    sent = []
    see = screens(SOLI_OPEN, {"sent": True}, SOLI_OPEN, {"sent": True})
    gw = Gateway(settings, store, desktop=FakeDesktop(), vision=see, poll_s=0.02)
    msg = next(t for t in toolset.build(gw) if t.__name__ == "whatsapp_message")
    r = msg("Soli", "hi", send=True)
    assert r["status"] == "expired"  # nobody said yes: nothing sent
    profile.apply(p := profile.Profile.load(settings.home), "confirm_sends", "off").save(settings.home)
    assert p.confirm_sends is False
    r = msg("Soli", "hi", send=True)
    assert r["status"] == "ok" and r["result"]["sent"]
    sent.append(r)


def test_no_typing_into_terminals_or_the_run_box():
    for front in (("powershell", "Windows PowerShell"), ("windowsterminal", "Terminal"), ("explorer", "Run")):
        with pytest.raises(DesktopError, match="could run commands"):
            FakeDesktop(front=front).check_safe()


def test_generic_tools_report_clearly(settings, store):
    desk = FakeDesktop()
    gw = Gateway(settings, store, desktop=desk, vision=screens({"found": True, "point": [500, 500], "label": "Send"}))
    listed = gw.request("list_windows", {})["result"]
    assert {"browser": "chrome", "tab": "(2) WhatsApp"} in listed["untrusted_browser_tabs"]
    assert gw.request("switch_to", {"name": "whatsapp"})["result"]["in_front"].startswith("(2) WhatsApp")
    assert gw.request("click_on", {"target": "the Send button"})["result"]["clicked"] == "Send"
    assert gw.request("switch_to", {"name": "spotify"})["status"] == "error"
    assert gw.request("press_keys", {"keys": "ctrl+shift+alt+win+x"})["status"] == "error"  # too many keys


def test_keys_and_coordinates():
    assert desktop.parse_keys("ctrl+alt+/") == [0x11, 0x12, 0xBF]
    assert desktop.parse_keys("Enter") == [0x0D]
    with pytest.raises(DesktopError):
        desktop.parse_keys("ctrl+banana")
    assert Shot(b"", 100, 50, 800, 600).to_screen([500, 250]) == (300, 350)


# ---------------------------------------------------------------- saying yes
def test_spoken_yes_and_no():
    assert assistant.yes_or_no("ابعتها", "send it") is True
    assert assistant.yes_or_no("تمام") is True and assistant.yes_or_no("yes please") is True
    assert assistant.yes_or_no("لا استنى", "no wait") is False
    assert assistant.yes_or_no("what time is it") is None
    q = assistant.approval_question("whatsapp_send", {"contact": "Soli", "message": "on my way"})
    assert "Soli" in q and "on my way" in q

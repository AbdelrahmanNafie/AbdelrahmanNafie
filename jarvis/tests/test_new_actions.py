import socket
from types import SimpleNamespace

import pytest

from jarvis import actions


@pytest.fixture
def opened():
    return []


@pytest.fixture
def gw(settings, store, opened, launched):
    from jarvis.gateway import Gateway
    return Gateway(settings, store, launcher=launched.append, opener=opened.append, poll_s=0.02)


# ---------------------------------------------------------------- web
def test_open_url_only_http(gw, opened):
    assert gw.request("open_url", {"url": "https://example.com/a?b=1"})["status"] == "ok"
    for bad in ("file:///C:/Windows/system32", "javascript:alert(1)", "ms-settings:", "example.com"):
        assert gw.request("open_url", {"url": bad})["status"] == "error", bad
    assert opened == ["https://example.com/a?b=1"]


def test_google_search_url(gw, opened):
    gw.request("web_search", {"query": "weather in Cairo"})
    assert opened == ["https://www.google.com/search?q=weather+in+Cairo"]


def _public_dns(monkeypatch, ip="93.184.216.34"):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *_: [(0, 0, 0, "", (ip, 0))])


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        status, headers, body = self.pages[url]
        return SimpleNamespace(status_code=status, headers=headers, content=body.encode(), encoding="utf-8",
                               is_redirect=300 <= status < 400)


def test_fetch_page_returns_clean_untrusted_text(settings, store, monkeypatch):
    from jarvis.gateway import Gateway
    _public_dns(monkeypatch)
    page = ("<html><head><title>Cats</title><script>steal()</script></head>"
            "<body><nav>menu</nav><p>Cats are small.</p><p>They purr.</p></body></html>")
    http = FakeHttp({"https://example.com/cats": (200, {"content-type": "text/html"}, page)})
    r = Gateway(settings, store, http_client=http).request("fetch_page", {"url": "https://example.com/cats"})
    res = r["result"]
    assert res["title"] == "Cats"
    assert "Cats are small." in res["untrusted_page_text"] and "steal" not in res["untrusted_page_text"]
    assert "menu" not in res["untrusted_page_text"]


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.1", "10.0.0.5", "169.254.169.254", "::1"])
def test_fetch_page_refuses_local_network(settings, store, monkeypatch, ip):
    from jarvis.gateway import Gateway
    _public_dns(monkeypatch, ip)
    r = Gateway(settings, store, http_client=FakeHttp({})).request("fetch_page", {"url": "http://router.local/"})
    assert r["status"] == "error" and "local network" in r["error"]


def test_fetch_page_checks_redirect_targets(settings, store, monkeypatch):
    from jarvis.gateway import Gateway
    ips = {"good.com": "93.184.216.34", "evil.local": "192.168.0.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *_: [(0, 0, 0, "", (ips[host], 0))])
    http = FakeHttp({"https://good.com/": (302, {"location": "http://evil.local/admin"}, "")})
    r = Gateway(settings, store, http_client=http).request("fetch_page", {"url": "https://good.com/"})
    assert r["status"] == "error" and http.asked == ["https://good.com/"]


# --------------------------------------------------------------- files
def test_search_and_open_files(settings, gw, opened):
    proj = settings.code_roots[0] / "cv"
    proj.mkdir()
    (proj / "Ahmed CV final.pdf").write_text("x")
    (proj / "notes.txt").write_text("x")
    r = gw.request("search_files", {"query": "cv final"})["result"]
    paths = [x["path"] for x in r["results"]]
    assert any(p.endswith("Ahmed CV final.pdf") for p in paths)
    assert gw.request("open_path", {"path": paths[0]})["status"] == "ok"
    assert opened == [paths[0]]


def test_search_outside_allowed_folders_refused(gw, tmp_path):
    r = gw.request("search_files", {"query": "x", "folder": str(tmp_path)})
    assert r["status"] == "error"


def test_open_path_refuses_programs(settings, gw, opened):
    exe = settings.workspace / "setup.exe"
    exe.write_text("MZ")
    for name in ("setup.exe", "run.bat", "x.ps1", "link.lnk"):
        (settings.workspace / name).write_text("x")
        assert gw.request("open_path", {"path": name})["status"] == "error", name
    assert opened == []
    assert gw.request("open_path", {"path": "."})["status"] == "ok"  # a folder is fine


# ------------------------------------------------------------ messages
def test_email_draft_is_opened_not_sent(settings, gw, opened):
    r = gw.request("draft_message", {"channel": "email", "to": "boss@company.com",
                                     "subject": "Late today", "body": "I'll be 10 min late & sorry"})
    assert r["status"] == "ok" and "Not sent" in r["result"]["note"]
    assert opened[0].startswith("mailto:boss@company.com?subject=Late%20today&body=I%27ll%20be")
    assert "%26" in opened[0]  # the & can't inject extra fields
    assert (settings.workspace / "drafts").iterdir()


def test_whatsapp_draft(gw, opened):
    gw.request("draft_message", {"channel": "whatsapp", "to": "+20 100 123 4567", "body": "أنا جاي"})
    assert opened[0].startswith("whatsapp://send?phone=201001234567&text=%D8")


def test_unknown_channel_refused(gw):
    assert gw.request("draft_message", {"channel": "sms", "body": "x"})["status"] == "error"


# ---------------------------------------------------------------- apps
INSTALLED = {"Google Chrome": "Chrome", "Visual Studio Code": "Code.Id", "WhatsApp": "5319.WhatsApp!App",
             "Notepad": "notepad", "Microsoft Edge": "Edge", "Calculator": "calc!App"}


@pytest.mark.parametrize("spoken,expected", [
    ("chrome", "Google Chrome"), ("vs code", "Visual Studio Code"), ("whats app", "WhatsApp"),
    ("WhatsApp", "WhatsApp"), ("calculater", "Calculator"), ("edge", "Microsoft Edge"),
])
def test_app_matching(spoken, expected):
    assert actions.match_app(spoken, INSTALLED)[0] == expected


def test_unknown_app(settings):
    with pytest.raises(actions.ActionError):
        actions.open_app(settings, "photoshop", launcher=lambda c: None, installed=INSTALLED)


def test_store_app_launches_through_shell_appsfolder(settings):
    cmds = []
    actions.open_app(settings, "whatsapp", launcher=cmds.append, installed=INSTALLED)
    assert cmds == [["explorer.exe", "shell:AppsFolder\\5319.WhatsApp!App"]]


# -------------------------------------------------------------- safety
def test_model_cannot_pass_internal_arguments(gw, launched):
    r = gw.request("open_app", {"name": "notepad", "launcher": "evil"})
    assert r["status"] == "error" and launched == []


def test_coding_needs_approval_and_a_project_folder(settings, store):
    from jarvis.gateway import Gateway
    from jarvis.policy import Verdict, decide
    assert decide("code_task", paused=False).verdict is Verdict.NEEDS_APPROVAL
    with pytest.raises(actions.ActionError, match="project folders"):
        actions.code_task(settings, str(settings.workspace), "fix it")


def test_keyboard_approval(settings, store, launched):
    from jarvis.gateway import Gateway
    asked = []
    gw = Gateway(settings, store, poll_s=0.01, on_approval_needed=asked.append, approval_key=lambda: True)
    gw.request("write_note", {"name": "old", "text": "x"})
    assert gw.request("delete_file", {"path": "notes/old.md"})["status"] == "ok"
    assert asked and "delete file" in asked[0]


def test_keyboard_rejection(settings, store):
    from jarvis.gateway import Gateway
    gw = Gateway(settings, store, poll_s=0.01, approval_key=lambda: False)
    gw.request("write_note", {"name": "keep", "text": "x"})
    assert gw.request("delete_file", {"path": "notes/keep.md"})["status"] == "rejected"


def test_fetch_page_network_failure_is_a_clean_error(settings, store, monkeypatch):
    import httpx

    from jarvis.gateway import Gateway
    _public_dns(monkeypatch)

    class Down:
        def get(self, url):
            raise httpx.ConnectError("offline")

    r = Gateway(settings, store, http_client=Down()).request("fetch_page", {"url": "https://example.com"})
    assert r["status"] == "error" and "couldn't load" in r["error"]


# ------------------------------------------------------- system control
def test_volume_and_media_keys(settings, store):
    from jarvis.gateway import Gateway
    pressed = []
    gw = Gateway(settings, store, press=pressed.append)
    assert gw.request("media_key", {"action": "volume_up", "times": 5})["status"] == "ok"
    assert gw.request("media_key", {"action": "play_pause", "times": 9})["status"] == "ok"
    assert pressed == [0xAF] * 5 + [0xB3]
    assert gw.request("media_key", {"action": "shutdown"})["status"] == "error"


def test_lock_screen(settings, store):
    from jarvis.gateway import Gateway
    locked = []
    assert Gateway(settings, store, lock=lambda: locked.append(1)).request("lock_screen", {})["status"] == "ok"
    assert locked == [1]


RUNNING = '[{"ProcessName":"chrome","MainWindowTitle":"YouTube - Google Chrome"},' \
          '{"ProcessName":"Code","MainWindowTitle":"jarvis - Visual Studio Code"},' \
          '{"ProcessName":"explorer","MainWindowTitle":"Downloads"}]'


def test_list_and_close_apps_with_approval(settings, store):
    from jarvis.gateway import Gateway
    killed = []
    gw = Gateway(settings, store, shell=lambda script, stdin=None: RUNNING, kill=killed.append,
                 approval_key=lambda: True, poll_s=0.01)
    apps = gw.request("list_running_apps", {})["result"]["apps"]
    assert [a["process"] for a in apps] == ["chrome", "Code", "explorer"]
    assert gw.request("close_app", {"name": "chrome"})["status"] == "ok"
    assert gw.request("close_app", {"name": "vs code"})["status"] == "ok"
    assert gw.request("close_app", {"name": "downloads"})["status"] == "error"  # explorer is protected
    assert killed == ["chrome", "Code"]


def test_close_app_waits_for_approval(settings, store):
    from jarvis.gateway import Gateway
    killed = []
    gw = Gateway(settings, store, shell=lambda script, stdin=None: RUNNING, kill=killed.append,
                 approval_key=lambda: False, poll_s=0.01)
    assert gw.request("close_app", {"name": "chrome"})["status"] == "rejected" and killed == []


def test_clipboard(settings, store):
    from jarvis.gateway import Gateway
    calls = []

    def shell(script, stdin=None):
        calls.append(stdin)
        return "copied text"
    gw = Gateway(settings, store, shell=shell)
    assert gw.request("clipboard_read", {})["result"]["untrusted_clipboard_text"] == "copied text"
    gw.request("clipboard_write", {"text": "مرحبا"})
    assert calls[-1] == "مرحبا"  # sent on stdin, not in the command line


def test_reminder_fires(settings, store):
    import time

    from jarvis.gateway import Gateway
    heard = []
    r = Gateway(settings, store, notify=heard.append).request("set_reminder", {"minutes": 0.001, "message": "tea"})
    assert r["status"] == "ok"
    time.sleep(0.3)
    assert heard == ["tea"]


def test_system_info_has_time():
    from jarvis import config
    assert "local_time" in actions.system_info(config.load())

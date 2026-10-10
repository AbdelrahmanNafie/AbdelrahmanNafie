"""Memory, personal database, profile, persona prompt, controls, briefing/monitor, self-update."""

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from jarvis import assistant, profile, quick, selfupdate, voice
from jarvis.gateway import Gateway


@pytest.fixture
def gw(settings, store):
    return Gateway(settings, store, poll_s=0.02)


# ---------------------------------------------------------------- memory
def test_remember_recall_forget(gw, store):
    r = gw.request("remember", {"text": "Works as a product designer on a fintech app", "category": "work"})
    assert r["status"] == "ok"
    gw.request("remember", {"text": "Works as a product designer on a fintech app", "category": "work"})
    assert len(store.memories()) == 1  # no duplicates
    found = gw.request("recall", {"query": "fintech"})["result"]["memories"]
    assert found[0]["category"] == "work"
    assert gw.request("forget", {"memory_id": found[0]["id"]})["status"] == "ok"
    assert store.memories() == []
    assert gw.request("remember", {"text": "x"})["status"] == "error"


# ------------------------------------------------------------ personal db
def test_personal_database(gw, store):
    a = gw.request("db_add", {"collection": "Tasks", "text": "Send the invoice", "due": "2026-10-20 18:30"})
    gw.request("db_add", {"collection": "ideas", "text": "Voice notes to tasks"})
    assert a["status"] == "ok" and a["result"]["collection"] == "tasks"
    found = gw.request("db_find", {"collection": "tasks"})["result"]
    assert found["records"][0]["due"] == "2026-10-20 18:30"
    assert found["collections"] == {"tasks": 1, "ideas": 1}
    rid = a["result"]["record_id"]
    assert gw.request("db_update", {"record_id": rid, "done": True})["status"] == "ok"
    assert gw.request("db_find", {"collection": "tasks"})["result"]["records"] == []
    assert len(gw.request("db_find", {"collection": "tasks", "include_done": True})["result"]["records"]) == 1
    assert gw.request("db_delete", {"record_id": rid})["status"] == "ok"
    assert store.find_records("tasks", include_done=True) == []
    for bad in ({"collection": "a;drop", "text": "x"}, {"collection": "tasks", "text": " "},
                {"collection": "tasks", "text": "x", "due": "next tuesday"}):
        assert gw.request("db_add", bad)["status"] == "error", bad


# ------------------------------------------------------------- profile
def test_set_preference_saves_and_notifies(settings, store):
    seen = []
    gw = Gateway(settings, store, on_profile=seen.append)
    assert gw.request("set_preference", {"key": "user name", "value": "Abdelrahman"})["status"] == "ok"
    assert gw.request("set_preference", {"key": "assistant_name", "value": "Nova"})["status"] == "ok"
    assert gw.request("set_preference", {"key": "voice", "value": "kore"})["status"] == "ok"
    p = profile.Profile.load(settings.home)
    assert (p.user_name, p.assistant_name, p.voice) == ("Abdelrahman", "Nova", "Kore")
    assert seen[-1].assistant_name == "Nova"
    assert gw.request("set_preference", {"key": "is_admin", "value": "yes"})["status"] == "error"
    assert gw.request("set_preference", {"key": "reply_language", "value": "klingon"})["status"] == "error"


def test_profile_survives_a_broken_file(settings):
    (settings.home / "profile.json").write_text("{not json")
    assert profile.Profile.load(settings.home).assistant_name == "Jarvis"


# -------------------------------------------------------------- persona
def test_prompt_knows_names_memories_tasks_and_language(settings, store):
    store.remember("Builds a voice assistant called Jarvis", "projects")
    store.add_record("tasks", "Send the invoice")
    store.add_reminder(time.time() + 600, "Call mom")
    p = profile.Profile(user_name="Abdelrahman", assistant_name="Nova", reply_language="same")
    text = quick.build_system_prompt(p, store.memories(), store.collections(), store.upcoming_reminders(), "now")
    assert "You are Nova, Abdelrahman's personal AI assistant" in text
    assert "Builds a voice assistant" in text and "tasks: 1" in text and "Call mom" in text
    assert "in the language they used" in text and "HEARD:" in text
    off = quick.build_system_prompt(profile.Profile(proactive=False), [], {}, [], "now")
    assert "ask their name" in off and "proactive suggestions OFF" in off


# ------------------------------------------------------------- controls
def test_sleep_and_restart_need_voice_mode(settings, store):
    assert Gateway(settings, store).request("go_to_sleep", {})["status"] == "error"
    control = assistant.Control()
    gw = Gateway(settings, store, control=control)
    assert gw.request("go_to_sleep", {})["status"] == "ok" and control.sleep_requested.is_set()
    assert gw.request("restart_jarvis", {})["status"] == "ok" and control.restart_requested.is_set()


def test_web_answer_is_marked_untrusted(settings, store):
    asked = []

    def fake_google(question, model):
        asked.append(question)
        return {"answer": "Sunny, 31°C", "sources": [{"title": "Weather", "url": "https://example.com"}]}

    r = Gateway(settings, store, ask_google=fake_google).request("web_answer", {"question": "weather in Cairo"})
    assert r["result"]["untrusted_web_answer"] == "Sunny, 31°C" and asked == ["weather in Cairo"]


def test_improve_myself_needs_approval(settings, store):
    ran = []
    gw = Gateway(settings, store, improver=lambda req, model: ran.append(req) or {"changed": False}, poll_s=0.02)
    r = gw.request("improve_myself", {"request": "Add a joke command please"})
    assert r["status"] in ("expired", "denied", "rejected") and ran == []  # nobody approved


# ------------------------------------------------- briefing & monitor
def test_briefing(store):
    now = time.time()
    store.add_record("tasks", "Late one", due_ts=now - 3600)
    store.add_record("tasks", "Later one")
    store.add_reminder(now + 3600, "Stand-up")
    text = assistant.briefing(profile.Profile(user_name="Abdelrahman"), store, now)
    assert "Abdelrahman" in text and "2 open tasks, 1 overdue" in text and "Stand-up" in text
    assert "tell me what to call you" in assistant.briefing(profile.Profile(), store, now)


def test_monitor_fires_reminders_and_warns_on_low_battery_once(settings, store):
    from jarvis.ui import NullUI

    battery = {"percent": 15, "plugged_in": False}
    m = assistant.Monitor(settings, store, NullUI(), battery=lambda: battery)
    store.add_reminder(time.time() - 1, "tea")
    store.add_reminder(time.time() - 3600, "old one")
    said = m.check()
    assert "Reminder: tea" in said and "Missed reminder: old one" in said
    assert any("battery is at 15" in s for s in said)
    assert m.check() == []  # nothing repeated
    battery["plugged_in"] = True
    m.check()
    battery["plugged_in"] = False
    assert any("battery" in s for s in m.check())  # warns again after a new discharge
    profile.Profile(proactive=False).save(settings.home)
    battery["plugged_in"] = True
    m.check()
    battery["plugged_in"] = False
    assert m.check() == []


# ---------------------------------------------------------------- voice
def test_split_for_speech():
    assert voice.split_for_speech("Short one.") == ["Short one."]
    parts = voice.split_for_speech("Here's the plan. Then we do more things carefully. Ok. Last bit here.")
    assert parts == ["Here's the plan. Then we do more things carefully. Ok.", "Last bit here."]
    assert voice.split_for_speech("") == []


def test_gemini_voice_quota_switches_to_windows_for_a_while(monkeypatch):
    calls, tries = [], []
    monkeypatch.setattr(voice.sys, "platform", "win32")

    def generate_content(**_):
        tries.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    spk = voice.Speaker("gemini", client=client, runner=lambda cmd, **kw: calls.append(kw["input"]))
    spk.say("one")
    spk.say("two")
    assert calls == ["one", "two"] and len(tries) == 2  # each speech model tried once, then cooldown


def test_speaker_voice_follows_profile():
    spk = voice.Speaker("gemini")
    spk.set_voice("Kore")
    assert (spk.voice, spk.gemini_voice) == ("gemini", "Kore")
    spk.set_voice("windows")
    assert spk.voice == "windows"
    off = voice.Speaker("off")
    off.set_voice("Kore")
    assert off.voice == "off"


# ---------------------------------------------------------- self-update
def _git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo" / "jarvis"  # like the real layout: the project is a subfolder
    (root / "jarvis").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "jarvis" / "policy.py").write_text("SAFE = True\n")
    (root / "jarvis" / "skills.py").write_text("JOKES = []\n")
    _git(tmp_path / "repo", "init", "-q")
    _git(tmp_path / "repo", "add", "-A")
    _git(tmp_path / "repo", "commit", "-qm", "start")
    return root


def _runner(edit, tests_ok=True):
    real = selfupdate._run

    def run(cmd, *, cwd, input=None, timeout=900):
        if cmd[0] == "claude":
            edit(cwd)
            return subprocess.CompletedProcess(cmd, 0, '{"result": "Added a joke list."}', "")
        if cmd[:3] == [sys.executable, "-m", "pytest"]:
            return subprocess.CompletedProcess(cmd, 0 if tests_ok else 1, "1 failed" if not tests_ok else "ok", "")
        return real(cmd, cwd=cwd, input=input, timeout=timeout)
    return run


def test_self_update_commits_when_tests_pass(repo):
    def edit(root):
        (root / "jarvis" / "skills.py").write_text("JOKES = ['knock knock']\n")
        (root / "tests" / "test_jokes.py").write_text("def test_x(): pass\n")

    r = selfupdate.improve("add jokes", root=repo, run=_runner(edit), claude_exe="claude")
    assert r["changed"] and r["commit"] and "Added a joke list" in r["summary"]
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert "Jarvis self-update" in _git(repo, "log", "-1", "--format=%s").stdout


def test_self_update_undoes_failed_tests(repo):
    def edit(root):
        (root / "jarvis" / "skills.py").write_text("broken(\n")
        (root / "jarvis" / "new.py").write_text("x = 1\n")

    with pytest.raises(selfupdate.SelfUpdateError, match="tests failed"):
        selfupdate.improve("break it", root=repo, run=_runner(edit, tests_ok=False), claude_exe="claude")
    assert (repo / "jarvis" / "skills.py").read_text() == "JOKES = []\n"
    assert not (repo / "jarvis" / "new.py").exists()


def test_self_update_refuses_to_touch_the_safety_core(repo):
    def edit(root):
        (root / "jarvis" / "policy.py").write_text("SAFE = False\n")

    with pytest.raises(selfupdate.SelfUpdateError, match="safety core"):
        selfupdate.improve("relax the rules", root=repo, run=_runner(edit), claude_exe="claude")
    assert (repo / "jarvis" / "policy.py").read_text() == "SAFE = True\n"


def test_self_update_needs_a_clean_tree(repo):
    (repo / "jarvis" / "skills.py").write_text("JOKES = ['wip']\n")
    with pytest.raises(selfupdate.SelfUpdateError, match="uncommitted"):
        selfupdate.improve("x", root=repo, run=_runner(lambda r: None), claude_exe="claude")

import threading
import time

from jarvis.policy import ACTION_RISK, Risk, Verdict, decide


def test_unknown_action_is_denied():
    assert decide("format_disk", paused=False).verdict is Verdict.DENY


def test_risk_tiers():
    assert decide("list_files", paused=False).verdict is Verdict.ALLOW
    assert decide("write_note", paused=False).verdict is Verdict.ALLOW
    assert decide("delete_file", paused=False).verdict is Verdict.NEEDS_APPROVAL


def test_kill_switch_blocks_everything_but_reads():
    for action, risk in ACTION_RISK.items():
        expected = Verdict.ALLOW if risk is Risk.READ else Verdict.DENY
        assert decide(action, paused=True).verdict is expected, action


def test_write_then_list_and_read(gateway):
    r = gateway.request("write_note", {"name": "shopping", "text": "milk, bread"})
    assert r["status"] == "ok"
    listing = gateway.request("list_files", {"folder": "notes"})
    assert [e["name"] for e in listing["result"]["entries"]] == ["shopping.md"]
    assert gateway.request("read_file", {"path": "notes/shopping.md"})["result"]["content"] == "milk, bread"


def test_paths_outside_workspace_are_refused(gateway, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password")
    for path in (str(secret), "../../secret.txt", "../jarvis.db"):
        r = gateway.request("read_file", {"path": path})
        assert r["status"] == "error" and "outside the allowed folders" in r["error"], path


def test_note_never_overwrites_and_name_is_validated(gateway):
    assert gateway.request("write_note", {"name": "a", "text": "1"})["status"] == "ok"
    assert gateway.request("write_note", {"name": "a", "text": "2"})["status"] == "error"
    assert gateway.request("write_note", {"name": "../evil", "text": "x"})["status"] == "error"


def test_open_app_only_known_apps(gateway, launched, monkeypatch):
    from jarvis import actions
    monkeypatch.setattr(actions, "_APPS_CACHE", {})  # no Start-menu apps in the test machine
    assert gateway.request("open_app", {"name": "notepad"})["status"] == "ok"
    assert gateway.request("open_app", {"name": "cmd.exe /c del *"})["status"] == "error"
    assert launched == [["notepad.exe"]]


def test_bad_arguments_from_model_are_reported(gateway):
    r = gateway.request("write_note", {"title": "x"})
    assert r["status"] == "error" and "bad arguments" in r["error"]


def _approve_when_pending(store, approved, delay=0.1):
    def worker():
        for _ in range(100):
            pending = store.pending_approvals()
            if pending:
                time.sleep(delay)
                store.decide(pending[0]["id"], approved)
                return
            time.sleep(0.02)
    t = threading.Thread(target=worker)
    t.start()
    return t


def test_delete_runs_only_after_human_approves(gateway, store, settings):
    gateway.request("write_note", {"name": "old", "text": "bye"})
    t = _approve_when_pending(store, approved=True)
    r = gateway.request("delete_file", {"path": "notes/old.md"})
    t.join()
    assert r["status"] == "ok"
    assert not (settings.workspace / "notes" / "old.md").exists()
    assert (settings.home / "trash").iterdir()  # recoverable


def test_delete_rejected_by_human_does_nothing(gateway, store, settings):
    gateway.request("write_note", {"name": "keep", "text": "x"})
    t = _approve_when_pending(store, approved=False)
    r = gateway.request("delete_file", {"path": "notes/keep.md"})
    t.join()
    assert r["status"] == "rejected"
    assert (settings.workspace / "notes" / "keep.md").exists()


def test_delete_expires_without_answer(gateway, settings):
    gateway.request("write_note", {"name": "keep", "text": "x"})
    r = gateway.request("delete_file", {"path": "notes/keep.md"})  # nobody clicks; wait is 2 s
    assert r["status"] == "expired"
    assert (settings.workspace / "notes" / "keep.md").exists()


def test_every_step_is_audited(gateway, store):
    gateway.request("system_info", {})
    kinds = [(e["stage"], e["kind"]) for e in store.events()]
    assert kinds == [("brain", "proposed"), ("policy", "allow"), ("hands", "executed")]


def test_stale_pending_requests_expire(store):
    req_id = store.create_approval("delete_file", {"path": "x"})
    assert store.expire_stale(max_age_s=3600) == 0
    assert store.expire_stale(max_age_s=-1) == 1
    assert store.approval_status(req_id) == "expired"
    assert store.decide(req_id, approved=True) is False  # a late click changes nothing

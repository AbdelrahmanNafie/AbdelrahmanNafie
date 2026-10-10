"""Single entry point between the brain and the hands.

propose -> policy decision -> (optional human approval) -> execute -> audit.
"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from . import actions
from .config import Settings
from .policy import ACTION_RISK, Verdict, decide
from .store import Store


class Gateway:
    def __init__(self, settings: Settings, store: Store, *, launcher: actions.Launcher | None = None,
                 opener: actions.Opener | None = None, http_client=None, press=None, lock=None,
                 shell=None, kill=None, notify=None, poll_s: float = 0.5,
                 on_approval_needed: Callable[[str], None] | None = None,
                 approval_key: Callable[[], bool | None] | None = None):
        self.settings = settings
        self.store = store
        # Side effects that tests (or other front-ends) can replace.
        self.effects = {k: v for k, v in
                        {"launcher": launcher, "opener": opener, "client": http_client, "press": press,
                         "lock": lock, "shell": shell, "kill": kill, "notify": notify}.items() if v}
        self.poll_s = poll_s
        # Voice mode: announce the request and accept a Y/N keypress besides the dashboard.
        self.on_approval_needed = on_approval_needed
        self.approval_key = approval_key
        assert set(ACTION_RISK) == set(actions.REGISTRY), "policy and actions must list the same names"

    def request(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        self.store.log("brain", "proposed", f"{action}({_fmt(args)})", action=action, args=args)
        decision = decide(action, paused=self.settings.paused)
        self.store.log("policy", decision.verdict.value, decision.reason, action=action,
                       risk=decision.risk.value if decision.risk else None)

        if decision.verdict is Verdict.DENY:
            return {"status": "denied", "reason": decision.reason}

        if decision.verdict is Verdict.NEEDS_APPROVAL:
            outcome = self._wait_for_human(action, args)
            if outcome != "approved":
                return {"status": outcome,
                        "reason": "the user did not approve this action; do not retry unless they ask"}

        return self._execute(action, args)

    def _wait_for_human(self, action: str, args: dict[str, Any]) -> str:
        req_id = self.store.create_approval(action, args)
        self.store.log("human", "approval_requested", f"waiting for approval #{req_id}: {action}({_fmt(args)})",
                       request_id=req_id)
        if self.on_approval_needed:
            self.on_approval_needed(f"{action.replace('_', ' ')} {_fmt(args)}")
        deadline = time.monotonic() + self.settings.approval_wait_s
        while time.monotonic() < deadline:
            if self.approval_key and (key := self.approval_key()) is not None:
                if self.store.decide(req_id, key):
                    self.store.log("human", "clicked", f"user {'approved' if key else 'rejected'} "
                                   f"#{req_id} by keyboard", request_id=req_id)
            status = self.store.approval_status(req_id)
            if status != "pending":
                break
            time.sleep(self.poll_s)
        else:
            if self.store.expire(req_id):
                status = "expired"
            else:  # decided in the last instant
                status = self.store.approval_status(req_id)
        self.store.log("human", status or "missing", f"approval #{req_id} {status}", request_id=req_id)
        return status or "expired"

    def _execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = actions.REGISTRY[action]
        params = inspect.signature(fn).parameters
        internal = {n for n, p in params.items() if p.kind is p.KEYWORD_ONLY}
        if bad := internal & set(args):  # e.g. a model trying to pass its own "launcher"
            self.store.log("hands", "failed", f"refused internal arguments {sorted(bad)}", action=action)
            return {"status": "error", "error": f"unknown arguments: {sorted(bad)}"}
        extra = {k: v for k, v in self.effects.items() if k in internal}
        try:
            result = fn(self.settings, **args, **extra)
        except actions.ActionError as exc:
            self.store.log("hands", "failed", str(exc), action=action)
            return {"status": "error", "error": str(exc)}
        except TypeError as exc:  # wrong/missing arguments from the model
            self.store.log("hands", "failed", f"bad arguments: {exc}", action=action)
            return {"status": "error", "error": f"bad arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001 — a failing action must never crash the assistant
            self.store.log("hands", "failed", f"{type(exc).__name__}: {exc}", action=action)
            return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        self.store.log("hands", "executed", f"{action} done", action=action, result=result)
        return {"status": "ok", "result": result}


def _fmt(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        parts.append(f"{k}={s[:60] + '…' if len(s) > 60 else s}")
    return ", ".join(parts)

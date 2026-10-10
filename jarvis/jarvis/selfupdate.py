"""Let Jarvis improve its own code — safely.

Flow (only after the user approved the request):
1. The Jarvis folder must have no uncommitted changes, so there's a clean point to return to.
2. Claude Code edits the code (file tools only: no shell, so it can't run anything).
3. Changes to the safety core (policy, gateway, this file) are refused and undone.
4. The full test suite runs. If anything fails, every change is undone.
5. If tests pass, the change is committed locally (never pushed). A restart loads it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the jarvis/ folder with pyproject.toml
PROTECTED = {"jarvis/policy.py", "jarvis/gateway.py", "jarvis/selfupdate.py"}

PROMPT = """\
You are improving the source code of "Jarvis", the user's personal voice assistant
(Python package in ./jarvis, tests in ./tests). The user asked:

{request}

Rules:
- Make the smallest change that does what was asked; match the existing style.
- Add or update tests in ./tests for new behaviour.
- Do NOT change jarvis/policy.py, jarvis/gateway.py or jarvis/selfupdate.py (safety core).
- Don't add network calls, credentials, or anything that sends data elsewhere.
- Finish with a 2-3 sentence summary of what you changed, in plain English.
"""

Runner = Callable[..., subprocess.CompletedProcess]


class SelfUpdateError(RuntimeError):
    pass


def _run(cmd: list[str], *, cwd: Path, input: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _git(run: Runner, root: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost", *args], cwd=root)


def _changed_files(run: Runner, root: Path) -> list[str]:
    out = _git(run, root, "status", "--porcelain", "--untracked-files=all", "--", ".").stdout
    files = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        files.append(path.split(" -> ")[-1])
    return files


def _undo(run: Runner, root: Path) -> None:
    _git(run, root, "checkout", "--", ".")
    _git(run, root, "clean", "-fd", "--", ".")  # untracked only; ignored files like .venv are kept


def improve(request: str, *, model: str = "sonnet", root: Path = PROJECT_ROOT, run: Runner = _run,
            claude_exe: str | None = None) -> dict:
    exe = claude_exe or shutil.which("claude")
    if exe is None:
        raise SelfUpdateError("Claude Code is not installed, so I can't edit my own code yet.")
    if _git(run, root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise SelfUpdateError("My folder isn't a git repository, so I couldn't undo a bad change. Not touching it.")
    if _changed_files(run, root):
        raise SelfUpdateError("My folder has uncommitted changes. Commit or discard them first, "
                              "so I always have a clean version to go back to.")

    edit = run([exe, "-p", "--output-format", "json", "--model", model, "--permission-mode", "acceptEdits",
                "--tools", "Read", "Edit", "Write", "Glob", "Grep"], cwd=root, input=PROMPT.format(request=request))
    summary = _claude_summary(edit.stdout)
    changed = _changed_files(run, root)
    if edit.returncode != 0:
        _undo(run, root)
        raise SelfUpdateError(f"Claude couldn't make the change: {summary or edit.stderr.strip()[:300]}")
    if not changed:
        return {"changed": False, "summary": summary or "Claude made no changes."}

    prefix = _git(run, root, "rev-parse", "--show-prefix").stdout.strip()  # e.g. "jarvis/"
    touched_core = sorted(PROTECTED & {_rel(prefix, f) for f in changed})
    if touched_core:
        _undo(run, root)
        raise SelfUpdateError(f"The change touched my safety core ({', '.join(touched_core)}), so I undid it.")

    tests = run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=root, timeout=600)
    if tests.returncode != 0:
        tail = "\n".join(tests.stdout.strip().splitlines()[-8:])
        _undo(run, root)
        raise SelfUpdateError(f"The tests failed after the change, so I undid it.\n{tail}")

    _git(run, root, "add", "-A", "--", ".")
    commit = _git(run, root, "commit", "-m", f"Jarvis self-update: {request[:72]}")
    sha = _git(run, root, "rev-parse", "--short", "HEAD").stdout.strip() if commit.returncode == 0 else ""
    return {"changed": True, "files": changed, "summary": summary, "commit": sha,
            "note": "Tests passed. Say 'restart' to load the new version. "
                    "Undo anytime with: git revert " + (sha or "<commit>")}


def _rel(prefix: str, path: str) -> str:
    """git porcelain paths are relative to the repo top; make them relative to the jarvis/ folder."""
    p = path.replace("\\", "/")
    return p[len(prefix):] if prefix and p.startswith(prefix) else p


def _claude_summary(stdout: str) -> str:
    import json

    try:
        return str(json.loads(stdout).get("result", ""))[:1500]
    except (json.JSONDecodeError, AttributeError):
        return ""

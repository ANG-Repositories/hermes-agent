"""Trigger assertions that respect the public-mirror workflow allowlist (card t_868e850a).

On ANG-Repositories/hermes-agent only the workflows named in .github/ci/public-workflow-allowlist.txt may
start runs by themselves; every other workflow is reduced to workflow_dispatch / workflow_call. A test that
pins a workflow's self-triggers (push, schedule, workflow_run, ...) calls ``self_triggers_live`` first: an
allowlisted workflow returns True and the test checks its triggers as before; a non-allowlisted one has the
reduction asserted here and returns False. Re-allowlisting a workflow (allowlist line + restored triggers in
one PR) turns its original trigger assertions back on with no test edit.
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
_LINT = ROOT / "scripts" / "lints" / "public-workflow-allowlist-lint.py"


def _lint():
    spec = importlib.util.spec_from_file_location("public_workflow_allowlist_lint", _LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def allowlisted(workflow: str, root: pathlib.Path = ROOT) -> bool:
    """True when ``.github/workflows/<workflow>`` is on the public-mirror allowlist."""
    return f".github/workflows/{workflow}" in _lint().load_allowlist(root)


def self_triggers_live(workflow: str, on, root: pathlib.Path = ROOT) -> bool:
    """Whether ``workflow``'s own triggers are live. When not allowlisted, assert ``on`` is dispatch/call only."""
    if allowlisted(workflow, root):
        return True
    lint = _lint()
    live = lint.triggers({"on": on}) - lint.INERT
    assert not live, f"{workflow}: not on the public-mirror allowlist but self-triggers on {sorted(live)}"
    return False

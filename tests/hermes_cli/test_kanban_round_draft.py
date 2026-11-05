"""Round draft (t_c418eebc): a Prism send-back re-draft is un-drafted once at the next handback.

The re-draft runs only on the Studio, in hermes-home's findings-route cron, which posts the PR comment line
``round-draft: re-drafted by findings-route at <head>``. This side reads that mark; it never re-drafts.
"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

CARD, SHA = "t_1234abcd", "a" * 40


def _jq_eval(jq: str, comments: list) -> dict:
    """Evaluate the mark the way GitHub's --jq would, for the one shape the helper sends."""
    line = json.loads(jq[jq.index("index([") + 7:jq.index("]) != null")])
    marked = any(line in [ln.rstrip("\r") for ln in (c.get("body") or "").split("\n")] for c in comments)
    return {"marked": marked}


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(op, "FLEET_OWNERS", {"o"})
    monkeypatch.setattr(fr, "worker_draft_repos", lambda: {"o/r"})
    monkeypatch.delenv("KANBAN_ROUND_DRAFT", raising=False)
    state = {"pr": {"draft": True, "head": {"sha": SHA, "ref": "worker/" + CARD}}, "comments": []}

    def gh(*args):
        if args[0] == "repos/o/r/pulls/1":
            return json.loads(json.dumps(state["pr"]))
        if args[0].startswith("repos/o/r/issues/1/comments"):
            return _jq_eval(args[2], state["comments"])
        return None
    calls = []

    def ready(*a):
        calls.append(a)
        state["pr"]["draft"] = False
        return 0
    opts = dict(task_id=CARD, allow_arm=False, gh=gh, flipped={"o/r#1": {"head": SHA, "at": 1}}, ready=ready)
    return SimpleNamespace(state=state, calls=calls, opts=opts, refs=op.extract_pr_refs("o/r#1"))


def test_route_mark_on_this_head_unholds_and_flips_once(rig):
    rig.state["comments"] = [{"body": "Prism round 1\r\nround-draft: re-drafted by findings-route at " + SHA}]
    report = fr.check(rig.refs, **rig.opts)
    assert rig.calls == [("o/r", 1)]
    assert report["draft_flipped"] == ["o/r#1"]
    fr.check(rig.refs, **rig.opts)  # PR is ready now: a second handback does not flip again
    assert rig.calls == [("o/r", 1)]


def test_no_mark_or_older_head_mark_is_still_an_operator_hold(rig):
    rig.state["comments"] = [{"body": "round-draft: re-drafted by findings-route at " + "b" * 40}]
    report = fr.check(rig.refs, **rig.opts)
    assert report["draft_held"] and rig.calls == []


def test_kill_switch_and_draft_ok_never_flip(rig, monkeypatch):
    rig.state["comments"] = [{"body": "round-draft: re-drafted by findings-route at " + SHA}]
    assert fr.check(rig.refs, draft_ok=True, **rig.opts)["draft_override"] == ["o/r#1"]
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "0")
    assert fr.check(rig.refs, **rig.opts)["draft_held"]
    assert rig.calls == []


def test_pr_naming_no_card_is_untouched(rig):
    rig.state["pr"]["head"]["ref"] = "feature/no-card"
    rig.state["comments"] = [{"body": "round-draft: re-drafted by findings-route at " + SHA}]
    try:
        fr.check(rig.refs, **rig.opts)
    except fr.DraftPrError:
        pass  # a foreign draft refuses the handback; either way nothing is flipped
    assert rig.calls == [] and rig.state["pr"]["draft"] is True


def test_ready_carries_owner_card_to_ace_gate(tmp_path, monkeypatch):
    import hermes_constants
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ace_gate.py").touch()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setenv("KANBAN_ROUND_DRAFT_CARD", "t_deadbeef")
    calls = []
    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0))
    assert fr.ace_gate_ready("o/r", 1, task_id=CARD) == 0
    assert fr.ace_gate_ready("o/r", 1) == 0
    assert [c["env"]["KANBAN_ROUND_DRAFT_CARD"] for c in calls] == [CARD, ""]

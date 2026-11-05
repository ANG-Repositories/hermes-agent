"""Round draft provenance reaches the handback's existing ready gate."""
import json
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import kanban_pr_freshness as fr
from hermes_cli import kanban_open_pr as op


def test_ready_carries_owner_without_inheriting_operator_identity(tmp_path, monkeypatch):
    import hermes_constants
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "scripts" / "ace_gate.py").touch()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    monkeypatch.setenv("KANBAN_ROUND_DRAFT_CARD", "t_deadbeef")
    calls = []
    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0))
    assert fr.ace_gate_ready("o/r", 1, task_id="t_1234abcd") == 0
    assert calls[0]["env"]["KANBAN_ROUND_DRAFT_CARD"] == "t_1234abcd"
    assert fr.ace_gate_ready("o/r", 1) == 0
    assert calls[1]["env"]["KANBAN_ROUND_DRAFT_CARD"] == ""


def test_same_head_round_can_handback_but_explicit_draft_ok_never_flips(tmp_path, monkeypatch):
    import hermes_constants
    root, card, sha = tmp_path, "t_1234abcd", "a" * 40
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    monkeypatch.setattr(op, "FLEET_OWNERS", {"o"})
    monkeypatch.setattr(fr, "worker_draft_repos", lambda: {"o/r"})
    path = root / "state" / "worker-round-draft" / "o__r__1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"card": card, "drafted_head": sha}))
    refs = op.extract_pr_refs("o/r#1")
    def gh(*args):
        if args[-1] == "repos/o/r/pulls/1":
            return {"draft": True, "head": {"sha": sha, "ref": "worker/" + card}}
        return None
    calls = []
    opts: dict = dict(task_id=card, allow_arm=False, gh=gh, flipped={"o/r#1": {"head": sha, "at": 1}},
                ready=lambda *a: calls.append(a) or 0)
    report = fr.check(refs, **opts)
    assert calls == [("o/r", 1)]
    assert report["draft_flipped"] == ["o/r#1"]
    calls.clear()
    assert fr.check(refs, draft_ok=True, **opts)["draft_override"] == ["o/r#1"]
    assert calls == []
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "0")
    assert fr.check(refs, **opts)["draft_held"]
    assert calls == []

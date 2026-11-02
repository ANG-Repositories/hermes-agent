"""public_workflow_allowlist_lint (t_a4fdb8ba): the public repo's workflow load guard. Red on a self-triggering
workflow with no allowlist line, a stale line, a reasonless callee and a live->disabled call; clean on this
repo's own .github/workflows; live mode flags GitHub state that disagrees with the file."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "public_workflow_allowlist_lint.py"
_spec = importlib.util.spec_from_file_location("pwal", SCRIPT)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)

CALLED = "on:\n  workflow_call:\njobs: {}\n"
PR = "on:\n  pull_request:\njobs:\n  j:\n    uses: ./.github/workflows/{callee}\n"


def _repo(tmp_path, files, allow):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files.items():
        (wf / name).write_text(text, encoding="utf-8")
    (tmp_path / ".github" / "ci").mkdir()
    (tmp_path / ".github" / "ci" / "public-workflow-allowlist.txt").write_text(
        "repo: o/r\n" + allow, encoding="utf-8")
    return tmp_path


def _found(tmp_path, files, allow):
    return L.static_findings(_repo(tmp_path, files, allow))[2]


def test_this_repo_is_clean():
    assert L.static_findings(ROOT)[2] == []


def test_every_self_triggering_workflow_needs_a_line(tmp_path):
    files = {"a.yml": PR.format(callee="c.yml"), "c.yml": CALLED, "new.yml": "on: [push]\njobs: {}\n"}
    got = _found(tmp_path, files, "live: a.yml\n")
    assert len(got) == 1 and got[0].startswith("new.yml:"), got


def test_workflow_call_only_needs_no_line(tmp_path):
    assert _found(tmp_path, {"a.yml": PR.format(callee="c.yml"), "c.yml": CALLED}, "live: a.yml\n") == []


def test_stale_line_and_reasonless_callee(tmp_path):
    got = _found(tmp_path, {"a.yml": "on: [push]\njobs: {}\n"},
                 "callee: a.yml\ndisabled: gone.yml\n")
    assert any("needs `# <why>`" in f for f in got), got
    assert any("gone.yml, which is not in" in f for f in got), got


def test_live_workflow_calling_a_disabled_one(tmp_path):
    files = {"a.yml": PR.format(callee="b.yml"), "b.yml": "on:\n  workflow_call:\n  push:\njobs: {}\n"}
    got = _found(tmp_path, files, "live: a.yml\ndisabled: b.yml\n")
    assert any("calls b.yml, which is `disabled`" in f for f in got), got


def test_malformed_and_missing_repo(tmp_path):
    repo, _e, errs = L.parse_allowlist("enable: a.yml\n")
    assert repo == "" and len(errs) == 2, errs


def test_live_state_drift():
    entries = {"a.yml": ("live", ""), "b.yml": ("disabled", "")}
    state = {"a.yml": (1, "disabled_manually"), "b.yml": (2, "active"), "c.yml": (3, "active")}
    assert L.live_findings(entries, state) == [("a.yml", "active"), ("b.yml", "disabled_manually")]
    ok = {"a.yml": (1, "active"), "b.yml": (2, "disabled_manually"), "c.yml": (3, "active")}
    assert L.live_findings(entries, ok) == []

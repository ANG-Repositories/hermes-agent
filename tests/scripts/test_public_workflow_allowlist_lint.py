"""public-workflow-allowlist-lint (card t_868e850a): the public mirror runs only the 11 allowlisted workflows."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LINT = ROOT / "scripts" / "lints" / "public-workflow-allowlist-lint.py"

EXPECTED = {
    "ci.yaml", "lint.yml", "js-tests.yml", "override-lint.yml", "docs-site-checks.yml",
    "case-collision-check.yml", "contributor-check.yml", "history-check.yml",
    "fleet-secret-scan.yml", "fleet-sast.yml", "cancel-superseded-pr-ci.yml",
}


def _mod():
    spec = importlib.util.spec_from_file_location("pwal", LINT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def tree(tmp_path):
    shutil.copytree(ROOT / ".github" / "workflows", tmp_path / ".github" / "workflows")
    (tmp_path / ".github" / "ci").mkdir(parents=True)
    shutil.copy(ROOT / ".github" / "ci" / "public-workflow-allowlist.txt", tmp_path / ".github" / "ci")
    return tmp_path


def test_allowlist_is_exactly_the_ruled_eleven():
    names = {p.rsplit("/", 1)[-1] for p in _mod().load_allowlist(ROOT)}
    assert names == EXPECTED


def test_repo_tree_is_clean():
    assert _mod().lint(ROOT) == []


def test_planted_enabled_workflow_reds(tree):
    (tree / ".github/workflows/planted.yml").write_text(
        "name: planted\non:\n  push:\n    branches: [main]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n")
    f = _mod().lint(tree)
    assert len(f) == 1 and "planted.yml" in f[0] and "push" in f[0]


def test_dispatch_and_call_only_workflow_passes(tree):
    (tree / ".github/workflows/inert.yml").write_text(
        "name: inert\non:\n  workflow_dispatch:\n  workflow_call:\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n")
    assert _mod().lint(tree) == []


def test_list_and_string_on_forms(tree):
    (tree / ".github/workflows/s.yml").write_text("on: schedule\njobs: {}\n")
    (tree / ".github/workflows/l.yml").write_text("on: [workflow_dispatch, pull_request]\njobs: {}\n")
    f = _mod().lint(tree)
    assert sorted(x.split(":")[0].rsplit("/", 1)[-1] for x in f) == ["l.yml", "s.yml"]


def test_ungated_self_hosted_in_allowlisted_callee_reds(tree):
    p = tree / ".github/workflows/lint.yml"
    p.write_text(p.read_text() + "  planted_sh:\n    runs-on: [self-hosted, ace-ai]\n    steps: [{run: 'true'}]\n")
    f = _mod().lint(tree)
    assert len(f) == 1 and "self-hosted" in f[0] and "planted_sh" in f[0]


def test_stale_allowlist_line_reds(tree):
    (tree / ".github/workflows/history-check.yml").unlink()
    assert any("missing" in x for x in _mod().lint(tree))


def test_kill_switch(monkeypatch, tree, capsys):
    (tree / ".github/workflows/planted.yml").write_text("on: push\njobs: {}\n")
    assert _mod().main(["--repo", str(tree)]) == 1
    monkeypatch.setenv("PUBLIC_WORKFLOW_ALLOWLIST", "off")
    assert _mod().main(["--repo", str(tree)]) == 0
    assert "skipped" in capsys.readouterr().out

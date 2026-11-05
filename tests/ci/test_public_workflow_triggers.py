"""self_triggers_live (t_d615c428): trigger tests follow the public-mirror allowlist (t_868e850a)."""
from __future__ import annotations

import pytest

from tests.ci.public_workflow_triggers import allowlisted, self_triggers_live


def _tree(tmp_path, allow: str):
    (tmp_path / ".github" / "ci").mkdir(parents=True)
    (tmp_path / ".github" / "ci" / "public-workflow-allowlist.txt").write_text(allow, encoding="utf-8")
    return tmp_path


def test_allowlisted_workflow_keeps_its_trigger_assertions(tmp_path):
    root = _tree(tmp_path, "# c\n.github/workflows/live.yml\n")
    assert allowlisted("live.yml", root)
    assert self_triggers_live("live.yml", {"push": {"branches": ["main"]}}, root) is True


def test_reduced_workflow_passes_dispatch_only(tmp_path):
    root = _tree(tmp_path, ".github/workflows/live.yml\n")
    assert self_triggers_live("off.yml", {"workflow_dispatch": None, "workflow_call": {}}, root) is False


@pytest.mark.parametrize("trigger", ["push", "schedule", "workflow_run", "pull_request"])
def test_reduced_workflow_with_a_self_trigger_is_red(tmp_path, trigger):
    root = _tree(tmp_path, ".github/workflows/live.yml\n")
    with pytest.raises(AssertionError, match=trigger):
        self_triggers_live("off.yml", {"workflow_dispatch": None, trigger: {}}, root)


def test_reduced_workflows_under_trigger_tests_are_off_the_allowlist():
    # Trigger tests on these files went red on main 2026-11-04 (t_d615c428) after #7 reduced them.
    for wf in ("archive-inputs.yml", "termux-verify.yml", "stable-release-publication.yml",
               "warm-selfhosted-cache.yml", "fleet-ci-fail-alert.yml"):
        assert not allowlisted(wf), wf

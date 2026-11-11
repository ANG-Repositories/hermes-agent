"""scripts/check_worktree_local_reasons.py (t_20fa16c7): no worktree card stays on the Studio for an
unenumerated reason. Ace 2026-11-10: "All machines can make any checkout always ... lint it so this can
never happen again."
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT / "hermes_cli" / "kanban_worktree_port.py"
DISPATCH = ROOT / "hermes_cli" / "kanban_db_dispatch.py"


def _lint():
    spec = importlib.util.spec_from_file_location("check_worktree_local_reasons",
                                                  ROOT / "scripts" / "check_worktree_local_reasons.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_tree_passes_and_every_reason_has_a_justification():
    lint = _lint()
    assert lint.main([str(PORT), str(DISPATCH)]) == 0
    from hermes_cli import kanban_worktree_port as kwt
    assert set(kwt.LOCAL_ONLY_REASONS) == set(lint.ENUMERATED)


def test_an_unlisted_local_only_reason_fails(tmp_path):
    src = PORT.read_text(encoding="utf-8-sig")
    port = tmp_path / "kanban_worktree_port.py"
    # A new code path keeping the card on the Studio because of where its checkout lives.
    port.write_text(src + '\n\ndef _new_rule(why):\n    _why(why, "checkout_lives_on_the_studio")\n',
                    encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1
    # ... and a WHY_ constant added to the module without the lint list.
    port.write_text(src.replace('WHY_PINNED = "operator_pin"', 'WHY_PINNED = "repo_on_the_list"'),
                    encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1


def test_a_fixed_repo_list_reason_in_the_dispatcher_fails(tmp_path):
    src = DISPATCH.read_text(encoding="utf-8-sig")
    dispatch = tmp_path / "kanban_db_dispatch.py"
    dispatch.write_text(src + '\n\ndef _old():\n    return "local", "not_portable:workspace_kind:worktree is '
                        'Studio-only; fleet/kanban-worktree-mirrors.json must list the repo"\n', encoding="utf-8")
    assert _lint().main([str(PORT), str(dispatch)]) == 1

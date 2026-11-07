"""Real mutation-seat coverage for the fleet citation checker (t_8f188124)."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_PROFILE", "HERMES_PROFILE_NAME",
                "RULING_CITATION_LINT"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_ruling_citation as guard
    target = home / "scripts/lints/ruling-citation-lint.py"
    target.parent.mkdir(parents=True)
    # The separately shipped predicate has its own tests; this fixture proves
    # that every core writer calls its API with the actual fields and board.
    target.write_text('''
import re

def direct_citation(text):
    return "1568681380928295105" in text

def check_fields(fields, comment_cites):
    for name, text in fields:
        if text and "Ace " in text and not direct_citation(text):
            refs = re.findall(r"comment (\\d+)", text)
            if not any(comment_cites(int(ref)) for ref in refs):
                return "BLOCKED by ruling-citation: add a message id"
''')
    monkeypatch.setattr(guard, "_fleet_root", lambda: home)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("verb", ["create", "edit", "comment"])
@pytest.mark.parametrize("cited,disabled", [(False, False), (True, False), (False, True)])
def test_mutations_check_real_text(board, monkeypatch, verb, cited, disabled):
    tid = kb.create_task(board, title="citation scratch", assignee="human", require_home=False)
    text = "Ace said ship it" + (" (Discord msg 1568681380928295105)" if cited else "")
    if disabled:
        monkeypatch.setenv("RULING_CITATION_LINT", "0")
    def write():
        if verb == "create":
            return kb.create_task(board, title="new scratch", body=text, assignee="human", require_home=False)
        if verb == "edit":
            return kb.edit_task(board, tid, body=text)
        return kb.add_comment(board, tid, "operator", text)
    before = board.total_changes
    if cited or disabled:
        assert write()
    else:
        with pytest.raises(ValueError, match="BLOCKED by ruling-citation"):
            write()
        assert board.total_changes == before
        assert kb.get_task(board, tid).body is None
        assert kb.list_comments(board, tid) == []


def test_comment_reference_uses_target_board(board):
    tid = kb.create_task(board, title="citation refs", assignee="human", require_home=False)
    good = kb.add_comment(board, tid, "operator", "source: 1568681380928295105")
    bad = kb.add_comment(board, tid, "operator", "no evidence")
    assert kb.edit_task(board, tid, body=f"Ace decided ship it (comment {good})")
    with pytest.raises(ValueError, match="ruling-citation"):
        kb.add_comment(board, tid, "operator", f"Ace decided ship it (comment {bad})")
    with pytest.raises(ValueError, match="ruling-citation"):
        kb.add_comment(board, tid, "operator", "Ace decided ship it (comment 999999)")

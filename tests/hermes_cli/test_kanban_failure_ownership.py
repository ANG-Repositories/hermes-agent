"""Disposable-board acceptance for the inert authority (never the fleet board)."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_failure_ownership as fo


@pytest.fixture
def board(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv('HERMES_HOME', str(root / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    path = root / 'board' / 'kanban.db'
    assert path.resolve().is_relative_to(root)
    conn = kb.connect(path)
    yield conn
    conn.close()


def key(node='tests/test_x.py::test_A'):
    return json.dumps([1, 'fleet', '', 'github.com', 1, 2, '.github/workflows/ci.yml', [],
                       [['os', 'linux']], 'suite', 'unit:invocation', 'pytest-node', node],
                      ensure_ascii=False, separators=(',', ':'))


def test_disabled_does_not_create_or_wake(board):
    ref = fo.publish_evidence(board, b'capture', verifier=lambda data: fo.Observation(
        key=key(), source_key='source-1', result='failure', sha='a'*40,
        run_id=1, attempt=1, job_id=1, lineage=('a'*40,), policy_generation=1))
    card = fo.prepare_card(board, title='repair', body='bounded action; acceptance: test_x',
                           assignee=None, session_id='operator')
    result = fo.observe_failure(board, ref, producer='ci-red-owner', card_spec=card)
    assert result.action == 'DISABLED'
    assert board.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert board.execute('SELECT count(*) FROM task_events').fetchone()[0] == 0

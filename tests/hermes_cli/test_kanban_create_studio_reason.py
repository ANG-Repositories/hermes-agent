"""Board create code runs hermes-home's studio-requires-reason on the REAL body (t_4743853f).

The pre_tool_call hook only sees one agent's own tool call, so a `hermes kanban create` from a
terminal/script or a pool host's `kanban_create` reached the board with a bare
`requires: studio-state`. Both create paths now ask the fleet root's hook module
(``create_refusal(body, root)``) and refuse with its text. Kill switch: STUDIO_REQUIRES_REASON=0
(read by the hook function itself).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# Stand-in for hermes-home hooks/access_bound_create_policy.py (hermes-home#10978 shape).
HOOK = '''
import os, re
def pool_hosts(root):
    return set()
def verdict(body, hosts):
    return None
def create_refusal(body, root):
    if os.environ.get("STUDIO_REQUIRES_REASON", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    if re.search(r"^requires:[ \\t]*studio-state[ \\t]*$", body or "", re.M):
        return "BLOCKED by studio-requires-reason: `requires: studio-state` needs a reason in parentheses"
    return None
'''

BARE = "do a thing\nrequires: studio-state\n"
REASONED = "do a thing\nrequires: studio-state (reads the live session database file)\n"
REFUSAL = "BLOCKED by studio-requires-reason"


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "access_bound_create_policy.py").write_text(HOOK, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("STUDIO_REQUIRES_REASON", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _count() -> int:
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _cli_create(tmp_path: Path, body: str) -> int:
    f = tmp_path / "body.md"
    f.write_text(body, encoding="utf-8")
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "probe", "--body-file", str(f), "--unhomed"])
    return kc._cmd_create(args)


def test_cli_create_refuses_a_bare_studio_state_line(fleet, tmp_path, capsys, monkeypatch):
    assert _cli_create(tmp_path, BARE) == 2
    err = capsys.readouterr().err
    print(err)
    assert f"kanban create: {REFUSAL}" in err
    assert _count() == 0
    assert _cli_create(tmp_path, REASONED) == 0
    assert _count() == 1
    monkeypatch.setenv("STUDIO_REQUIRES_REASON", "0")
    assert _cli_create(tmp_path, BARE) == 0
    assert _count() == 2


def test_tool_create_refuses_a_bare_studio_state_line(fleet, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    out = json.loads(kt._handle_create({"board": "default", "title": "probe", "assignee": "peer", "body": BARE,
                                        "unhomed": True}))
    print(out)
    assert REFUSAL in json.dumps(out)
    assert not out.get("ok")
    assert _count() == 0
    ok = json.loads(kt._handle_create({"board": "default", "title": "probe", "assignee": "peer", "body": REASONED,
                                       "unhomed": True}))
    assert ok.get("ok") is True, ok
    assert _count() == 1


def test_no_hook_module_fails_open(fleet, tmp_path):
    (fleet / "hooks" / "access_bound_create_policy.py").unlink()
    assert _cli_create(tmp_path, BARE) == 0

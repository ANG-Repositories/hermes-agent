"""Capture -> verify -> delete for DONE cards whose workspace the survivor gate holds (t_83c38f7b).

A done card closed with ``--survivor-none`` keeps its scratch tree forever: completion cleanup refuses
("survivor-none cannot authorize workspace deletion") and nothing ever captured the files. The reclaim
path stores them as attachments on the card, re-reads and verifies them, then deletes through the same
choke point. A card that is not done, or a capture that does not verify, keeps its tree.
"""
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_reclaim as kr
from hermes_cli import kanban_survivor as ks


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, check=True).stdout.decode().strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_temporary_roots", lambda: [tmp_path / "temporary"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_process_cwd_within", lambda path: False)  # the test runner's own cwd scan
    with kb.connect_closing() as conn:
        yield conn


def held_done_card(conn):
    """A scratch card closed survivor-none with real work in its tree: held on base."""
    follow = kb.create_task(conn, title="follow-up")
    tid = kb.create_task(conn, title="scratch card with evidence")
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, ws)
    (ws / "pr-body.md").write_text("the PR body nobody pushed\n")
    (ws / "probes").mkdir()
    (ws / "probes" / "census.py").write_text("print('census')\n")
    (ws / "probes" / "__pycache__").mkdir()
    (ws / "probes" / "__pycache__" / "census.cpython-312.pyc").write_bytes(b"\x00pyc")
    assert kb.complete_task(conn, tid, summary="closed", survivor_none=True,
                            survivor_reason=f"evidence only; follow-up {follow}")
    assert ws.is_dir()  # the base behaviour this card fixes: held forever
    held = conn.execute("SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?", (tid,)).fetchone()[0]
    assert kr.reclaimable(held), held
    return tid, ws


def test_held_done_card_is_captured_verified_and_deleted(board):
    tid, ws = held_done_card(board)
    rows = kr.sweep(board, dry_run=False, min_age_days=0)
    assert [r["outcome"] for r in rows] == ["reclaimed"], rows
    assert not ws.exists()
    tars = [a for a in kb.list_attachments(board, tid) if a.filename.endswith(".tar.gz")]
    assert len(tars) == 1 and tars[0].filename.startswith(f"workspace-reclaim-{tid}-")
    with tarfile.open(fileobj=io.BytesIO(Path(tars[0].stored_path).read_bytes()), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        assert tar.extractfile("pr-body.md").read() == b"the PR body nobody pushed\n"
    assert names == ["pr-body.md", "probes/census.py"]  # residue is not captured
    row = board.execute("SELECT held_reason, survivor FROM task_workspace_survivors WHERE task_id = ?",
                        (tid,)).fetchone()
    survivor = json.loads(row[1])
    assert row[0] is None and survivor["kind"] == "reclaimed"
    assert survivor["ref"] == f"reclaimed:{tars[0].id}"
    assert survivor["previous"]["kind"] == "none"  # the earlier record is kept, not overwritten
    events = [e for e in kb.list_events(board, tid) if e.kind == "workspace_reclaimed"]
    assert len(events) == 1 and events[0].payload["attachment_ids"] == [tars[0].id]
    assert events[0].payload["bytes_freed"] > 0


def test_dry_run_writes_nothing(board):
    tid, ws = held_done_card(board)
    rows = kr.sweep(board, dry_run=True, min_age_days=0)
    assert rows[0]["outcome"] == "would-reclaim" and rows[0]["files"] == 2
    assert ws.is_dir() and not kb.list_attachments(board, tid)


def test_card_in_review_is_not_touched(board):
    tid, ws = held_done_card(board)
    board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    board.commit()
    assert kr.sweep(board, dry_run=False, min_age_days=0) == []
    c = kr.Candidate(tid, "review", str(ws), "survivor_unavailable: survivor-none cannot authorize workspace "
                     "deletion", None, 0)
    assert kr.reclaim_one(board, c, dry_run=False)["outcome"] == "skipped"
    assert ws.is_dir() and not kb.list_attachments(board, tid)


def test_capture_whose_sha_does_not_verify_keeps_the_tree(board, monkeypatch):
    tid, ws = held_done_card(board)
    real_store = ks._store

    def corrupting_store(conn, task_id, name, data, content_type):
        ref = real_store(conn, task_id, name, data, content_type)
        Path(ref["path"]).write_bytes(data + b"bitrot")  # the bytes on disk are not what was hashed
        return ref
    monkeypatch.setattr(ks, "_store", corrupting_store)
    rows = kr.sweep(board, dry_run=False, min_age_days=0)
    assert rows[0]["outcome"] == "held" and "sha" in rows[0]["why"]
    assert (ws / "pr-body.md").is_file()
    held = board.execute("SELECT held_reason FROM task_workspace_survivors WHERE task_id = ?", (tid,)).fetchone()[0]
    assert held.startswith("survivor_unavailable: reclaim held (") and "survivor-none" in held


def test_tree_changed_after_capture_is_held(board, monkeypatch):
    tid, ws = held_done_card(board)
    real_capture = kr.capture

    def late_write(conn, task_id, p):
        out = real_capture(conn, task_id, p)
        (ws / "late.txt").write_text("written after the capture\n")
        return out
    monkeypatch.setattr(kr, "capture", late_write)
    assert kr.sweep(board, dry_run=False, min_age_days=0)[0]["outcome"] == "held"
    assert (ws / "late.txt").is_file()


def test_dirty_repo_diff_and_untracked_file_are_captured(board, tmp_path):
    tid, ws = held_done_card(board)
    repo = ws / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    git(repo, "push", "-q", "origin", "HEAD:main")
    git(repo, "fetch", "-q", "origin")
    (repo / "code.py").write_text("value = 2\n")
    (repo / "notes.md").write_text("untracked\n")
    rows = kr.sweep(board, dry_run=False, min_age_days=0)
    assert rows[0]["outcome"] == "reclaimed", rows
    patches = [a for a in kb.list_attachments(board, tid) if a.filename.endswith(".patch")]
    assert len(patches) == 1 and b"+value = 2" in Path(patches[0].stored_path).read_bytes()
    tar = next(a for a in kb.list_attachments(board, tid) if a.filename.endswith(".tar.gz"))
    with tarfile.open(tar.stored_path, mode="r:gz") as t:
        assert "repo/notes.md" in t.getnames() and "repo/code.py" not in t.getnames()


def test_unpushed_commit_is_held(board, tmp_path):
    tid, ws = held_done_card(board)
    repo = ws / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "code.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "local only")
    rows = kr.sweep(board, dry_run=False, min_age_days=0)
    assert rows[0]["outcome"] == "held" and "local commits" in rows[0]["why"]
    assert (repo / "code.py").is_file()


def test_cli_apply_needs_the_operator_token(board, capsys, monkeypatch):
    tid, ws = held_done_card(board)
    monkeypatch.delenv(kb.OPERATOR_TOKEN_ENV, raising=False)
    args = kb_args(apply=True)
    assert kr.run(args) == 1
    assert ws.is_dir()
    assert kr.run(kb_args()) == 0
    assert "would-reclaim" in capsys.readouterr().out and ws.is_dir()


def kb_args(**kw):
    from types import SimpleNamespace
    base = dict(apply=False, dry_run=False, limit=None, min_age_days=0, as_json=False)
    base.update(kw)
    return SimpleNamespace(**base)

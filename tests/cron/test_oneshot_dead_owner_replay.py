"""A one-shot whose owner was SIGTERMed mid-run is replayed exactly once (t_b5c315bb).

2026-11-08: one-shot 2a450ce62b25 fired 17:32:59, the gateway (pid 74812) was SIGTERMed at
17:39:04 and the drain interrupted the run. The record kept fire_claim + run_claim owned by the
dead pid, repeat.completed=1 and last_run_at=None. Every later tick logged
cron.oneshot.overdue_not_due naming the dead claim and skipped the job: a claim, any claim,
made _retire_expired_oneshot keep it "in case a run is still in flight elsewhere". The job
never fired and nothing paged.

These tests kill a REAL owner process with SIGTERM mid-run, then drive the due scan in the
test process (the "new owner") and pin:
  - the job is returned exactly once (claims released, repeat.completed rolled back once);
  - a second scan, and a scan after the replay completes, do not fire it again;
  - a replay that is itself killed is not replayed a second time;
  - a dead-owner wedge that persists for more than 2 ticks pages #alerts once per episode.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
JOB_ID = "oneshot-sigterm"

_OWNER = textwrap.dedent(
    f"""
    import sys, time
    from cron.jobs import claim_dispatch, claim_job_for_fire, get_due_jobs
    from cron.executions import create_execution, mark_execution_running

    due = [j["id"] for j in get_due_jobs()]
    assert due == [{JOB_ID!r}], due
    assert claim_job_for_fire({JOB_ID!r})
    assert claim_dispatch({JOB_ID!r})
    row = create_execution({JOB_ID!r}, source="tick")
    assert mark_execution_running(row["id"]) is not None
    print("RUNNING", flush=True)
    time.sleep(600)
    """
)


def _store(home: Path) -> dict:
    from cron.jobs import load_jobs

    return next(j for j in load_jobs() if j["id"] == JOB_ID)


def _seed(home: Path, run_at: datetime) -> None:
    from cron.jobs import save_jobs

    save_jobs([{
        "id": JOB_ID,
        "name": JOB_ID,
        "prompt": "",
        "no_agent": True,
        "script": "oneshot/x.sh",
        "schedule": {"kind": "once", "run_at": run_at.isoformat()},
        "next_run_at": run_at.isoformat(),
        "last_run_at": None,
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": 1, "completed": 0},
        "deliver": "local",
    }])


def _sigterm_owner_mid_run(home: Path) -> int:
    """Run the claim + dispatch in a separate owner process and SIGTERM it mid-run."""
    env = {k: v for k, v in os.environ.items() if k != "HERMES_MACHINE_ID"}
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        [sys.executable, "-c", _OWNER], cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        if line.strip() != "RUNNING":
            proc.kill()
            _, err = proc.communicate(timeout=30)
            pytest.fail(f"owner never reached the run: {line!r} {err[-2000:]}")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
    return proc.pid


@pytest.fixture()
def home(hermes_env, monkeypatch):
    import cron.jobs as jobs_mod

    monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {"cron": {}})
    monkeypatch.setattr(jobs_mod, "_overdue_oneshot_warned", {})
    monkeypatch.setattr(jobs_mod, "_dead_owner_wedge_ticks", {})
    monkeypatch.setattr(jobs_mod, "_dead_owner_wedge_paged", set())
    pages = []
    monkeypatch.setattr(jobs_mod, "_dispatch_dead_owner_page", lambda argv: pages.append(argv))
    return SimpleNamespace(path=hermes_env, pages=pages)


def _shift_clock(monkeypatch, seconds: float) -> None:
    import cron.jobs as jobs_mod

    real = jobs_mod._hermes_now
    later = real() + timedelta(seconds=seconds)
    monkeypatch.setattr(jobs_mod, "_hermes_now", lambda: later)


# 5 s: the next tick right after the restart. 4493 s: the measured overdue_not_due age.
@pytest.mark.parametrize("next_tick_s", [5, 4493])
def test_sigterm_mid_run_oneshot_replays_exactly_once(home, monkeypatch, next_tick_s):
    import cron.jobs as jobs_mod
    from cron.executions import latest_execution

    _seed(home.path, datetime.now(timezone.utc).astimezone() - timedelta(seconds=1))
    dead_pid = _sigterm_owner_mid_run(home.path)

    killed = _store(home.path)
    # The measured wedge shape: both claims owned by the dead pid, the dispatch counted, no run.
    assert killed["run_claim"]["by"].endswith(f":{dead_pid}")
    assert f":{dead_pid}:" in killed["fire_claim"]["by"]
    assert killed["repeat"]["completed"] == 1
    assert killed["last_run_at"] is None
    assert latest_execution(JOB_ID)["status"] == "running"

    _shift_clock(monkeypatch, next_tick_s)
    due = [j["id"] for j in jobs_mod.get_due_jobs()]
    assert due == [JOB_ID]

    replayed = _store(home.path)
    assert replayed["fire_claim"] is None
    assert replayed["run_claim"]["by"].endswith(f":{os.getpid()}")  # the new owner's claim
    assert replayed["repeat"]["completed"] == 0
    assert replayed[jobs_mod.RESTART_REQUEUE_COUNT_KEY] == 1
    # The killed attempt is recorded honestly, not left "running" forever.
    assert latest_execution(JOB_ID)["status"] == "unknown"

    # A second tick while the replay runs must not double-fire.
    assert [j["id"] for j in jobs_mod.get_due_jobs()] == []

    # The replay runs and completes: never fires again.
    assert jobs_mod.claim_dispatch(JOB_ID)
    jobs_mod.mark_job_run(JOB_ID, True)
    assert [j["id"] for j in jobs_mod.get_due_jobs()] == []
    done = _store(home.path) if any(j["id"] == JOB_ID for j in jobs_mod.load_jobs()) else None
    if done is not None:
        assert done["last_run_at"] is not None
        assert done["repeat"]["completed"] == 1
    assert home.pages == []


def test_replay_killed_again_is_not_replayed_and_pages_once(home, monkeypatch):
    import cron.jobs as jobs_mod

    _seed(home.path, datetime.now(timezone.utc).astimezone() - timedelta(seconds=1))
    _sigterm_owner_mid_run(home.path)
    # The replay budget was already spent by an earlier interrupted replay.
    raw = jobs_mod.load_jobs()
    raw[0][jobs_mod.RESTART_REQUEUE_COUNT_KEY] = 1
    jobs_mod.save_jobs(raw)

    _shift_clock(monkeypatch, 4493)
    for _ in range(5):
        assert [j["id"] for j in jobs_mod.get_due_jobs()] == []

    # Wedged for more than 2 ticks with a dead-owner claim: exactly one page, for this job.
    assert len(home.pages) == 1
    argv = home.pages[0]
    assert argv[0] == "page"
    key = argv[argv.index("--key") + 1]
    assert key == "%s:%s" % (jobs_mod._dead_owner_episode_scope(), JOB_ID)
    text = argv[argv.index("--text") + 1]
    assert JOB_ID in text and "dead" in text


_PAUSED_OWNER = textwrap.dedent(
    f"""
    import time
    from cron.jobs import claim_dispatch, claim_job_for_fire, get_due_jobs

    due = [j["id"] for j in get_due_jobs()]
    assert due == [{JOB_ID!r}], due
    assert claim_job_for_fire({JOB_ID!r})
    assert claim_dispatch({JOB_ID!r})
    print("CLAIMED", flush=True)  # paused before create_execution: no ledger row yet
    time.sleep(600)
    """
)


def test_live_owner_with_expired_claims_and_no_ledger_row_is_not_replayed(home, monkeypatch):
    """Prism 9e9a051f53d3: TTL expiry is not death. A live owner paused between claim_dispatch
    and create_execution must not have its one-shot replayed under it."""
    import cron.jobs as jobs_mod
    from cron.executions import latest_execution

    _seed(home.path, datetime.now(timezone.utc).astimezone() - timedelta(seconds=1))
    env = {k: v for k, v in os.environ.items() if k != "HERMES_MACHINE_ID"}
    env["HERMES_HOME"] = str(home.path)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        [sys.executable, "-c", _PAUSED_OWNER], cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        if line.strip() != "CLAIMED":
            proc.kill()
            _, err = proc.communicate(timeout=30)
            pytest.fail(f"owner never claimed: {line!r} {err[-2000:]}")
        assert latest_execution(JOB_ID) is None
        # Both claims far past their TTLs while the owner process is still alive.
        _shift_clock(monkeypatch, 4 * 3600)
        for _ in range(4):
            assert [j["id"] for j in jobs_mod.get_due_jobs()] == []
        assert proc.poll() is None
        job = next((j for j in jobs_mod.load_jobs() if j["id"] == JOB_ID), None)
        if job is not None:
            assert job["repeat"]["completed"] == 1
            assert not job.get(jobs_mod.RESTART_REQUEUE_COUNT_KEY)
        assert latest_execution(JOB_ID) is None
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_startup_recovery_preserves_the_replay(home, monkeypatch):
    """Prism 36f55300e879: scheduler startup runs recover_interrupted() before the first due
    scan; its jobs.json reconcile must arm the replay, not record the run and retire the job."""
    import cron.jobs as jobs_mod
    from cron.executions import latest_execution
    from cron.scheduler_provider import InProcessCronScheduler

    _seed(home.path, datetime.now(timezone.utc).astimezone() - timedelta(seconds=1))
    _sigterm_owner_mid_run(home.path)

    assert InProcessCronScheduler().recover_interrupted() >= 1
    recovered = _store(home.path)
    assert recovered["last_run_at"] is None
    assert recovered["run_claim"] is None and recovered["fire_claim"] is None
    assert recovered["repeat"]["completed"] == 0
    assert recovered[jobs_mod.RESTART_REQUEUE_COUNT_KEY] == 1
    assert latest_execution(JOB_ID)["status"] == "unknown"

    _shift_clock(monkeypatch, 5)
    assert [j["id"] for j in jobs_mod.get_due_jobs()] == [JOB_ID]
    assert [j["id"] for j in jobs_mod.get_due_jobs()] == []  # no double fire
    assert home.pages == []


def test_dead_owner_episodes_are_isolated_per_store(home, monkeypatch, tmp_path):
    """Prism 4e78fd657d34: another profile's scan of a job with the same id neither counts nor
    clears this store's open episode."""
    import cron.jobs as jobs_mod

    _seed(home.path, datetime.now(timezone.utc).astimezone() - timedelta(seconds=1))
    _sigterm_owner_mid_run(home.path)
    raw = jobs_mod.load_jobs()
    raw[0][jobs_mod.RESTART_REQUEUE_COUNT_KEY] = 1
    jobs_mod.save_jobs(raw)
    _shift_clock(monkeypatch, 4493)
    for _ in range(3):
        jobs_mod.get_due_jobs()
    assert [a[0] for a in home.pages] == ["page"]
    key_a = home.pages[0][home.pages[0].index("--key") + 1]

    other = tmp_path / "other-profile"
    with jobs_mod.use_cron_store(other):
        healthy = dict(raw[0], run_claim=None, fire_claim=None)
        jobs_mod._warn_overdue_oneshots_not_due([healthy], [], set(), jobs_mod._hermes_now())
    assert [a[0] for a in home.pages] == ["page"]  # store A's episode was not cleared
    assert key_a in jobs_mod._dead_owner_wedge_paged
    with jobs_mod.use_cron_store(other):
        assert jobs_mod._dead_owner_episode_scope() != key_a.rsplit(":", 1)[0]

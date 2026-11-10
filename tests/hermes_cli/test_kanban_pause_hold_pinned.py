"""An fwa hold freezes only cards PINNED to the held host (t_72362742).

56 placement_wait ``pause_hold (nas-fleet:fwa:pause:...)`` in 2 h on 2026-11-09: the hold was keyed on
``origin_host`` (the host the LAST run used), so a ``host: any`` card that last ran on the paused host sat for the
whole pause. Now: a pinned card is held (today's behaviour), an unpinned one is re-placed by the allocator
(``placement_rerouted`` ``origin_host_paused``), and the origin hint is honoured again once the hold ends.
``KANBAN_PAUSE_FOLLOWS_ORIGIN=1`` restores the origin-keyed hold.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pause_hold as kph

from tests.hermes_cli.test_kanban_pause_hold import (  # noqa: F401  (fixtures + helpers of the base suite)
    _no_real_spawn, _plan, _runs, _set, _tick, home,
)
from tests.hermes_cli.test_kanban_pause_hold_fwa import HID, _hold, _neutral_local_host  # noqa: F401


def _card(conn, body, origin="ci-box"):
    tid = kb.create_task(conn, title="card", assignee="alpha", body=body)
    _set(conn, tid, status="ready", origin_host=origin)
    return tid


def _pause_waits(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='placement_wait' "
                        "AND json_extract(payload,'$.code')='pause_hold'", (tid,)).fetchone()[0]


def _reroutes(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kph.REROUTE_EVENT))]


@pytest.fixture
def enforce(monkeypatch):
    """placement.continuation: enforce (live), so origin_route returns ("origin", host) for a reachable origin."""
    real = kbd._root_kanban_cfg

    def cfg():
        out = dict(real() or {})
        out["placement"] = {**(out.get("placement") or {}), "continuation": "enforce"}
        return out
    monkeypatch.setattr(kbd, "_root_kanban_cfg", cfg)


def _enforced_plan(conn, tids):
    plan = _plan()
    plan.continuation_states = {
        ("ci-box", r[0]): "nonempty"
        for r in conn.execute("SELECT workspace_path FROM tasks WHERE id IN (%s)" % ",".join("?" * len(tids)),
                              tids)}
    return plan


@pytest.mark.parametrize("body", ["do the thing", "host: any\ndo the thing"], ids=["no-pin", "host-any"])
def test_unpinned_card_on_a_held_origin_is_placed_elsewhere(home, body):
    with kb.connect_closing() as conn:
        tid = _card(conn, body)
        _hold(home)
        plan = _plan()
        plan.default_remote = True                          # remote-first: the pool picks among ci-box / ace-ai
        res, spawned = _tick(conn, plan)
        assert (tid, "ace-ai") in spawned, (spawned, res.placement_waits.get(tid))
        assert _pause_waits(conn, tid) == 0


@pytest.mark.parametrize("body", ["do the thing", "host: any\ndo the thing"], ids=["no-pin", "host-any"])
def test_unpinned_origin_route_is_rerouted_under_continuation_enforce(home, enforce, body):
    with kb.connect_closing() as conn:
        tid = _card(conn, body)
        _hold(home)
        plan = _enforced_plan(conn, [tid])
        plan.default_remote = True
        res, spawned = _tick(conn, plan)
        assert (tid, "ace-ai") in spawned, (spawned, res.placement_waits.get(tid))
        assert _pause_waits(conn, tid) == 0
        ev = _reroutes(conn, tid)
        assert len(ev) == 1 and ev[0]["reason"] == "origin_host_paused" and ev[0]["from"] == "ci-box", ev
        assert ev[0]["hold"].startswith(f"pause_hold (ci-box:fwa:pause:{HID} "), ev


def test_reroute_event_is_written_once_per_hold_episode(home):
    with kb.connect_closing() as conn:
        tid = _card(conn, "do the thing")
        _hold(home)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        for _ in range(3):
            assert kbd._reroute_off_held_origin(conn, row, "ci-box", {}) is None
        assert len(_reroutes(conn, tid)) == 1
        assert kbd._reroute_off_held_origin(conn, row, "ci-box", {}, dry_run=True) is None
        assert len(_reroutes(conn, tid)) == 1


@pytest.mark.parametrize("pin", ["ci-box", "CI-Box"])
def test_card_pinned_to_the_held_host_is_still_held(home, pin):
    with kb.connect_closing() as conn:
        tid = _card(conn, f"host: {pin}\ndo the thing")
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert _pause_waits(conn, tid) >= 1
        assert _runs(conn, tid) == 0
        assert _reroutes(conn, tid) == []


def test_pinned_card_is_held_under_continuation_enforce(home, enforce):
    with kb.connect_closing() as conn:
        tid = _card(conn, "host: ci-box\ndo the thing")
        _hold(home)
        _res, spawned = _tick(conn, _enforced_plan(conn, [tid]))
        assert tid not in {t for t, _ in spawned}
        assert _pause_waits(conn, tid) >= 1
        assert _reroutes(conn, tid) == []


def test_layer2_refuses_only_a_pinned_card(home):
    with kb.connect_closing() as conn:
        pinned = _card(conn, "host: ci-box\nx")
        loose = _card(conn, "host: any\nx")
        _hold(home)
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, pinned)
        assert kb.claim_task(conn, loose) is not None


def test_hold_lifted_honours_the_origin_hint_again(home, enforce):
    with kb.connect_closing() as conn:
        tid = _card(conn, "do the thing")
        _hold(home).unlink()                                # the hold is archived: no live fwa hold
        res, spawned = _tick(conn, _enforced_plan(conn, [tid]))
        assert (tid, "ci-box") in spawned, (spawned, res.placement_waits.get(tid))
        assert _reroutes(conn, tid) == []


def test_expired_hold_honours_the_origin_hint(home, enforce):
    with kb.connect_closing() as conn:
        tid = _card(conn, "do the thing")
        now = int(time.time())
        _hold(home, since=now - 7200, until=now - 1)
        _res, spawned = _tick(conn, _enforced_plan(conn, [tid]))
        assert (tid, "ci-box") in spawned, spawned


def test_kill_switch_restores_the_origin_keyed_hold(home, enforce, monkeypatch):
    monkeypatch.setenv(kph.FOLLOWS_ORIGIN_ENV, "1")
    with kb.connect_closing() as conn:
        tid = _card(conn, "host: any\ndo the thing")
        _hold(home)
        _res, spawned = _tick(conn, _enforced_plan(conn, [tid]))
        assert tid not in {t for t, _ in spawned}
        assert _pause_waits(conn, tid) >= 1
        assert _reroutes(conn, tid) == []
        _set(conn, tid, status="ready", claim_lock=None)
        with pytest.raises(kph.PauseHeld):
            kb.claim_task(conn, tid)


def test_review_card_keeps_the_origin_keyed_hold(home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="r", assignee="alpha", body="host: any\nx")
        _set(conn, tid, status="review", origin_host="ci-box")
        _hold(home)
        res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert res.placement_waits.get(tid, "").startswith(f"pause_hold (ci-box:fwa:pause:{HID}")


def test_legacy_manifest_hold_stays_origin_keyed(home):
    from tests.hermes_cli.test_kanban_pause_hold import _manifest
    with kb.connect_closing() as conn:
        tid = _card(conn, "host: any\nx")
        _manifest(home)
        _res, spawned = _tick(conn, _plan())
        assert tid not in {t for t, _ in spawned}
        assert _pause_waits(conn, tid) >= 1


@pytest.mark.parametrize("body,want", [
    ("x", False), ("host: any\nx", False), ("host: ace-ai\nx", False), ("host: ci-box\nx", True),
    ("host: CI-BOX\nx", True),
])
def test_pinned_to(home, body, want):
    assert kph.pinned_to(body, "ci-box", {}) is want

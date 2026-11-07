"""FWA hold records in the dispatcher (FWA pause/resume SPEC §5.1-§5.3, §9.1 AC1/AC4/AC8; card B3 t_0b56b707).

The fork re-implements hermes-home ``fwa.valid`` + ``fwa.accepting``; parity is the golden fixture set under
``tests/fixtures/fwa/`` (byte copies of hermes-home's), pinned by ``kanban_pause_hold.FWA_FIXTURES_SHA``.
Dispatcher arms reuse the hermetic board of ``test_kanban_pause_hold`` (real non-dry ``dispatch_once``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pause_hold as kph

from tests.hermes_cli.test_kanban_pause_hold import (  # noqa: F401  (fixtures + helpers of the base suite)
    MID, PLANS, _cards, _manifest, _no_real_spawn, _plan, _routes, _runs, _set, _tick, home,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fwa"
HID = "20261107T150000Z-ci-box-fwa00001"


def _fixtures_sha(d: Path) -> str:
    """SPEC §5.1 RC-5: sha256 over the sorted ``(name, bytes)`` of ``*.json`` (name, NUL, bytes, NUL per file)."""
    h = hashlib.sha256()
    for p in sorted(d.glob("*.json"), key=lambda p: p.name):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def _hold(home, host="ci-box", *, lanes=("kanban", "ci", "prism"), since=None, until=None, hid=HID, raw=None,
          **extra):
    d = home / "state" / "fwa" / "holds" / host
    d.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    doc = {"schema": 1, "id": hid, "host": host, "kind": "pause", "lanes": list(lanes), "owner": "card:t_x",
           "drive": True, "by": "apollo", "card": "t_x", "reason": "fixture", "since": now - 60 if since is None
           else since, "until": now + 3600 if until is None else until}
    doc.update(extra)
    p = d / f"{hid}.json"
    p.write_text(raw if raw is not None else json.dumps(doc), encoding="utf-8")
    return p


# -- AC8: fixture contract ---------------------------------------------------------------------------------------

def test_fixture_dir_hashes_to_the_published_constant():
    assert FIXTURES.is_dir() and list(FIXTURES.glob("*.json"))
    assert _fixtures_sha(FIXTURES) == kph.FWA_FIXTURES_SHA


def test_constant_is_readable_as_text_by_the_home_sweep():
    """§5.4 step 5 reads the constant out of this file with one regex, never by import."""
    import re
    text = Path(kph.__file__).read_text(encoding="utf-8-sig")
    m = re.findall(r'^FWA_FIXTURES_SHA = "([0-9a-f]{64})"$', text, re.M)
    assert m == [kph.FWA_FIXTURES_SHA]


CASES = sorted(p.name for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", CASES)
def test_fork_predicate_matches_every_golden_fixture(home, name):
    case = json.loads((FIXTURES / name).read_text(encoding="utf-8-sig"))
    host, now = case["host"], float(case["now"])
    d = kph.fwa_root() / host
    d.mkdir(parents=True, exist_ok=True)
    for h in case["holds"]:
        if "file" in h:                      # a file as bytes on disk: {file, raw, age_s} (unreadable / invalid)
            p = d / h["file"]
            p.write_text(h["raw"], encoding="utf-8")
        else:                                # a hold record, written as <id>.json
            p = d / f"{h['id']}.json"
            p.write_text(json.dumps(h), encoding="utf-8")
        mt = now - float(h.get("age_s", 0))   # mtime is case data: the unreadable bound is mtime + 24 h
        os.utime(p, (mt, mt))
    entries = kph.fwa_entries(host, {("fwa", "table"): {}})
    for lane, want in case["expect"].items():
        got = kph.fwa_verdict(entries, lane, now)
        if want is True:
            assert got is None, (name, lane, got)
        elif want is False:
            assert got is not None and got[0] != kph.UNREADABLE, (name, lane, got)
        else:
            assert want == "unreadable" and got is not None and got[0] == kph.UNREADABLE, (name, lane, got)


# -- AC1 dispatcher cell: an fwa hold alone holds both layers ------------------------------------------------------

@pytest.mark.parametrize("mkplan", PLANS)
def test_fwa_hold_with_no_manifest_holds_every_card_of_the_host(home, mkplan):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        res, spawned = _tick(conn, mkplan())
        routes = _routes(res)
        got = {t for t, _h in spawned}
        for key in "ABDF":
            r = routes[ids[key]]
            assert r["outcome"] == "held" and r["code"] == "pause_hold", (key, r)
            assert r["detail"].startswith(f"ci-box:fwa:pause:{HID} by apollo until "), r["detail"]
            assert ids[key] not in got and _runs(conn, ids[key]) == 0
        assert ids["C"] in got                                    # another host's card is untouched
        assert res.placement_waits[ids["E"]].startswith(f"pause_hold (ci-box:fwa:pause:{HID}")
        assert ids["E"] not in got and _runs(conn, ids["E"]) == 0


def test_layer2_refuses_a_card_held_only_by_fwa(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        with pytest.raises(kph.PauseHeld) as ei:
            kb.claim_task(conn, ids["A"])
        assert ei.value.host == "ci-box" and HID in ei.value.manifest_id
        assert _runs(conn, ids["A"]) == 0


@pytest.mark.parametrize("lanes", [("ci",), ("reboot", "deploy"), ("prism", "ci-failover")])
def test_hold_without_lane_kanban_admits(home, lanes):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, lanes=lanes)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_expired_hold_never_holds(home):
    """I3: ``now >= until`` = accepting, even before the sweep archives the file."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        now = int(time.time())
        _hold(home, since=now - 7200, until=now - 1)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


@pytest.mark.parametrize("raw", ["{not json", json.dumps({"schema": 1, "id": "x", "host": "ci-box",
                                                          "lanes": ["kanban"], "since": 1})], ids=["junk", "no-until"])
def test_unreadable_or_endless_hold_file_holds_fail_closed(home, raw):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home, raw=raw)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and r["detail"].startswith("ci-box:fwa:unreadable:"), r
        assert ids["A"] not in {t for t, _ in spawned}


def test_unreadable_hold_file_stops_holding_24h_after_its_mtime(home):
    """§5.2: a fail-closed file has an end too."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        p = _hold(home, raw="{not json")
        old = time.time() - kph.FWA_UNREADABLE_MAX_S - 5
        os.utime(p, (old, old))
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_lock_and_dot_files_are_not_holds(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        d = home / "state" / "fwa" / "holds" / "ci-box"
        d.mkdir(parents=True)
        (d / ".lock").write_text("", encoding="utf-8")
        (d / ".tmp.json").write_text("{not json", encoding="utf-8")
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_profile_home_fwa_hold_is_never_read(home):
    """§5.1 root rule: a hold under a PROFILE home must not hold (nor be the place the dispatcher looks)."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        prof = home / "profiles" / "alpha"
        _hold(prof)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


def test_alias_directory_holds_the_canonical_host(home):
    """§5.2: ``ci``, ``ci.ace``, ``ci-box`` name one host."""
    _hold(home, host="ci")
    cache = {("fwa", "table"): {"ci-box": {"ci-box", "ci", "ci.ace"}}}
    v = kph.decide("ci-box", "t_any", cache)
    assert v is not None and v[0] == "wait" and HID in v[1]
    assert kph.decide("ace-ai", "t_any", {("fwa", "table"): {"ci-box": {"ci-box", "ci"}}}) is None


# -- AC4: never cached across ticks ---------------------------------------------------------------------------------

def test_removing_the_hold_file_releases_on_the_next_tick(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        p = _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] not in {t for t, _ in spawned}
        p.unlink()
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}


# -- dual-read (§5.3, §8 P1) and the kill switch (§11) --------------------------------------------------------------

def test_legacy_manifest_still_holds_with_no_fwa_hold(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home)
        res, _spawned = _tick(conn, _plan())
        assert _routes(res)[ids["A"]]["detail"] == f"ci-box:{MID}"


def test_fwa_hold_beats_a_resume_pin(home):
    """A held host takes no run, pinned or not: a resumed manifest's owed pin does not route a card onto it."""
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _manifest(home, "resumed", [{"lane": "kanban", "id": ids["A"], "resume": {"new_run_id": None}}])
        _hold(home)
        res, spawned = _tick(conn, _plan())
        r = _routes(res)[ids["A"]]
        assert r["outcome"] == "held" and "fwa:" in r["detail"]
        assert ids["A"] not in {t for t, _ in spawned}


def _source(monkeypatch, value):
    monkeypatch.setattr(kb, "_board_home_kanban_cfg", lambda: {"placement": {"pause_hold_source": value}})


def test_kill_switch_manifest_ignores_fwa_holds(home, monkeypatch):
    _source(monkeypatch, "manifest")
    assert kph.source() == kph.SOURCE_MANIFEST
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _hold(home)
        _res, spawned = _tick(conn, _plan())
        assert ids["A"] in {t for t, _ in spawned}
        assert kph.holds(conn)["fwa"] == {}


@pytest.mark.parametrize("value,want", [("fwa", "fwa"), ("FWA", "fwa"), ("bogus", "fwa"), (None, "fwa"),
                                        ("manifest", "manifest"), (" Manifest ", "manifest")])
def test_source_parses_fail_closed(monkeypatch, value, want):
    _source(monkeypatch, value)
    assert kph.source() == want


# -- resume pin lapse (§5.3: resumed_at + 2 h, stamped or not) -------------------------------------------------------

def _resumed(home, tid, resumed_at):
    d = home / "state" / "fleet-pause" / "ci-box"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema": 2, "id": MID, "host": "ci-box", "phase": "resumed", "resumed_at": resumed_at,
        "units": [{"lane": "kanban", "id": tid, "resume": {"new_run_id": None}}]}), encoding="utf-8")


def test_resume_pin_lapses_two_hours_after_resumed_at(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - kph.PIN_LAPSE_S - 5)
        assert kph.decide("ci-box", ids["A"], {}) is None
        res, _spawned = _tick(conn, _plan())
        assert _routes(res)[ids["A"]]["route"] != "remote-pin"


def test_resume_pin_holds_inside_the_lapse_window(home):
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - 60)
        assert kph.decide("ci-box", ids["A"], {}) == ("pin", "ci-box")


def test_resume_pin_lapse_is_off_under_the_kill_switch(home, monkeypatch):
    _source(monkeypatch, "manifest")
    with kb.connect_closing() as conn:
        ids = _cards(conn)
        _resumed(home, ids["A"], int(time.time()) - kph.PIN_LAPSE_S - 5)
        assert kph.decide("ci-box", ids["A"], {}) == ("pin", "ci-box")


# -- read model ------------------------------------------------------------------------------------------------------

def test_placement_holds_lists_fwa_holds(home):
    _hold(home)
    _hold(home, hid="20261107T150000Z-ci-box-fwa00002", lanes=("ci",))
    rep = kph.holds()
    rows = {r["id"]: r for r in rep["fwa"]["ci-box"]}
    assert rows[HID]["holds_kanban"] is True
    assert rows["20261107T150000Z-ci-box-fwa00002"]["holds_kanban"] is False
    assert rep["source"] == "fwa"

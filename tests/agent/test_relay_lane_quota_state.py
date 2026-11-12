"""t_d780729d: a Claude relay lane keeps no per-lane quota verdict.

Every Claude lane reaches the same subscriptions through a relay that knows
each seat's state. A 429 seen on one lane name must not leave that lane (or a
sibling name of the same pool) believing something the relay does not, and a
stored "exhausted" must never outlive a stated reset or a short cache.
"""

from __future__ import annotations

import json
import time

import pytest

from agent import fallback_policy as fp
from agent.fallback_sticky_store import StickyKey, StickyStore
from agent.relay_lanes import is_relay_backed, is_relay_pinned, is_relay_pooled

T0 = 1_790_000_000.0


# ── which names are relay-backed ──────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "claude-apr", "claude-alr", "claude-alrs", "claude-alrf", "claude-dalrs",
    "claude-dalrf", "claude-bpr", "claude-btpr", "claude-dtlr", "claude-dtlrs",
    "claude-dtlrf", "claude-bhpr", "claude-bpr-tui", "CLAUDE-APR",
])
def test_pooled_relay_faces(name):
    assert is_relay_pooled(name) and is_relay_backed(name)
    assert not is_relay_pinned(name)


@pytest.mark.parametrize("name", ["claude-apx-0", "claude-apx-7", "claude-bpx-12",
                                  "claude-cpx-3", "claude-alx-4", "claude-dtlx-2"])
def test_seat_pins(name):
    assert is_relay_pinned(name) and is_relay_backed(name)
    assert not is_relay_pooled(name)


@pytest.mark.parametrize("name", ["anthropic", "claude-code", "claude", "openai-codex",
                                  "claude-apr-3", "claude-apx", "claude-opus-5-5", "", None])
def test_not_relay_lanes(name):
    assert not is_relay_backed(name)


# ── credential pool: no stored quota exhaustion for relay lanes ───────────

def _auth(tmp_path, monkeypatch, pools: dict) -> None:
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": pools}))


def _row(cid, key, **status):
    return {"id": cid, "label": cid, "auth_type": "api_key", "priority": 0,
            "source": "manual", "access_token": key, **status}


def _disk_row(tmp_path, provider, cid):
    data = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    return next(r for r in data["credential_pool"][provider] if r["id"] == cid)


@pytest.mark.parametrize("provider", ["claude-alr", "claude-apr", "claude-apx-7", "claude-dtlr"])
def test_429_on_relay_lane_is_not_persisted_as_exhausted(tmp_path, monkeypatch, provider):
    _auth(tmp_path, monkeypatch, {provider: [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    pool = load_pool(provider)
    assert pool.select() is not None
    nxt = pool.mark_exhausted_and_rotate(status_code=429, credential_id="r1",
                                         api_key_hint="sk-relay", failure_reason="rate_limit")
    assert nxt is None  # nothing to rotate to: the relay rotates seats, not us
    row = _disk_row(tmp_path, provider, "r1")
    assert row.get("last_status") != "exhausted", row
    assert load_pool(provider).has_available() is True


def test_429_on_one_lane_name_leaves_sibling_names_agreeing(tmp_path, monkeypatch):
    """alr and apr are the same relay: a 429 on alr must not make alr believe
    'out' while apr believes 'ok' (Discord 1570522498922053854)."""
    _auth(tmp_path, monkeypatch, {"claude-alr": [_row("a", "sk-relay")],
                                  "claude-apr": [_row("b", "sk-relay")]})
    from agent.credential_pool import load_pool

    load_pool("claude-alr").mark_exhausted_and_rotate(
        status_code=429, credential_id="a", api_key_hint="sk-relay", failure_reason="rate_limit")
    assert load_pool("claude-alr").has_available() == load_pool("claude-apr").has_available() is True


@pytest.mark.parametrize("status", [
    # the live shape: 429, no reset time at all, status written a minute ago
    {"last_status": "exhausted", "last_error_code": 429, "last_status_at": "NOW-60"},
    # a reset stamped on the lane row, still in the future
    {"last_status": "exhausted", "last_error_code": 429, "last_status_at": "NOW-60",
     "last_error_reset_at": "NOW+86400"},
    # no timestamps at all
    {"last_status": "exhausted", "last_error_code": 429},
])
def test_stored_exhausted_row_does_not_bench_relay_lane(tmp_path, monkeypatch, status):
    now = time.time()
    status = {k: (now - 60 if v == "NOW-60" else now + 86400 if v == "NOW+86400" else v)
              for k, v in status.items()}
    _auth(tmp_path, monkeypatch, {"claude-apx-3": [_row("p3", "sk-pin", **status)]})
    from agent.credential_pool import load_pool

    pool = load_pool("claude-apx-3")
    assert pool.has_available() is True
    assert pool.next_available_at() is None
    assert pool.select() is not None
    # selection cleared the stale verdict on disk: nothing left to clean by hand
    assert _disk_row(tmp_path, "claude-apx-3", "p3").get("last_status") != "exhausted"


def test_relay_lane_auth_401_still_benches_the_local_key(tmp_path, monkeypatch):
    """A 401 is the LOCAL relay key, not a seat: that stays a local fact."""
    _auth(tmp_path, monkeypatch, {"claude-apr": [_row("r1", "sk-relay")]})
    from agent.credential_pool import load_pool

    load_pool("claude-apr").mark_exhausted_and_rotate(
        status_code=401, credential_id="r1", api_key_hint="sk-relay", failure_reason="auth")
    assert _disk_row(tmp_path, "claude-apr", "r1").get("last_status") == "exhausted"
    assert load_pool("claude-apr").has_available() is False


def test_non_relay_provider_429_still_benched(tmp_path, monkeypatch):
    _auth(tmp_path, monkeypatch, {"deepseek": [_row("d1", "sk-a"), _row("d2", "sk-b")]})
    from agent.credential_pool import load_pool

    load_pool("deepseek").mark_exhausted_and_rotate(
        status_code=429, credential_id="d1", api_key_hint="sk-a", failure_reason="rate_limit")
    assert _disk_row(tmp_path, "deepseek", "d1").get("last_status") == "exhausted"


def test_pre_exhausted_relay_row_does_not_skip_same_route_retry(tmp_path, monkeypatch):
    """_recover_rate_limit rotated 'immediately' on a stale exhausted row,
    skipping the one same-route retry and re-stamping the row."""
    from agent import agent_runtime_helpers as arh

    _auth(tmp_path, monkeypatch, {"claude-alr": [_row(
        "r1", "sk-relay", last_status="exhausted", last_error_code=429,
        last_status_at=time.time() - 30 * 86400)]})
    from agent.credential_pool import load_pool

    pool = load_pool("claude-alr")
    calls = []
    out = arh._recover_rate_limit(
        pool, has_retried_429=False, error_context=None, api_key_hint="sk-relay",
        credential_id="r1", rotate_and_swap=lambda *a: calls.append(a) or False)
    assert calls == [] and out == (False, True)


# ── sticky fallback: quota verdict = the relay's reset or a short cache ──

def _arm(store, provider, cls, **kw):
    key = StickyKey.build("root", provider, "claude-fable-5-2")
    st = fp.arm_sticky(store, key, cls=cls, now=T0, failing=(provider, "claude-fable-5-2"),
                       fallback=("claude-bpr", "claude-opus-5-5"), jitter=1.0, **kw)
    return st.until_epoch - T0


@pytest.mark.parametrize("provider", ["claude-alr", "claude-bpr", "claude-dtlr", "claude-btpr"])
def test_relay_quota_model_without_relay_reset_is_a_short_cache(tmp_path, provider):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, provider, "quota_model") == fp.RELAY_QUOTA_CACHE_S


def test_relay_quota_model_uses_relay_stated_reset(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "claude-alr", "quota_model", stated_reset_s=7200.0) == 7200.0


def test_relay_quota_seat_never_doubles(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    first = _arm(store, "claude-alr", "quota_seat", retry_after_s=90.0)
    second = _arm(store, "claude-alr", "quota_seat", retry_after_s=90.0)
    assert first == second == 90.0
    assert _arm(StickyStore(db_path=tmp_path / "b.db"), "claude-alr", "quota_seat") == fp.RELAY_QUOTA_CACHE_S


def test_relay_quota_cache_capped_at_window_ceiling(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "claude-apr", "quota_model", stated_reset_s=30 * 86400.0) == fp.WINDOW_CEILING_DEFAULT_S


def test_non_relay_and_direct_pin_cooldowns_unchanged(tmp_path):
    store = StickyStore(db_path=tmp_path / "turns.db")
    assert _arm(store, "openrouter", "quota_model") == 6 * 3600
    assert _arm(StickyStore(db_path=tmp_path / "p.db"), "claude-bpx-12", "quota_model") == 6 * 3600


def test_relay_quota_class_skips_the_local_legacy_bench():
    for cls in ("quota_seat", "quota_model"):
        assert fp.skips_legacy_arm(cls, "claude-alr") is True
        assert fp.skips_legacy_arm(cls, "claude-dtlr") is True
    assert fp.skips_legacy_arm("quota_model", "openrouter") is False
    assert fp.skips_legacy_arm("quota_model", "claude-bpx-12") is False
    assert fp.skips_legacy_arm("rate_upstream", "claude-alr") is False
    assert fp.skips_legacy_arm("conn", "openrouter") is True  # unchanged: skips_quota_gate

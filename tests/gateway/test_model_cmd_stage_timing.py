"""``/model`` per-stage timing line (t_7fe48f0b).

2026-11-09: an idle-session ``/m opus-d`` took ~79 s and ``switch_model()`` alone 0.09-3.6 s; nothing
said where the rest went. These drive the REAL ``_handle_model_command`` (the live fork override in
gateway/slash_commands.py) with a mocked switch pipeline and pin: exactly one ``PHASE=model_cmd`` INFO
line per command, every field present, the ``record_*`` split naming the slow store call, the
runner-wide lock wait, and fail-open telemetry.
"""

import asyncio
import logging
import re

import pytest
from unittest.mock import AsyncMock

from gateway import model_cmd_timing as mct
from tests.gateway.test_model_command_context_offload import _event, _runner_with_store


def _phase_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("PHASE=model_cmd ")]


def _fields(line):
    return dict(re.findall(r"(\w+)=(\S+)", line))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    r = _runner_with_store(tmp_path, monkeypatch)

    async def _slow_override(*_a, **_k):
        await asyncio.sleep(0.25)  # the slow store write the split must name

    r._async_session_store.set_model_override = AsyncMock(side_effect=_slow_override)
    return r


@pytest.mark.asyncio
async def test_typed_switch_logs_one_line_with_every_stage(runner, caplog):
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        reply = await runner._handle_model_command(_event("/model gpt-5.5"))
    assert reply and "gpt-5.5" in reply
    lines = _phase_lines(caplog)
    assert len(lines) == 1, lines
    f = _fields(lines[0])
    assert f["outcome"] == "ok" and f["session"] != "-", f
    for name in mct.PHASES + mct.DURABLE_SPANS + ("record", "other", "total"):
        assert f"{name}_s" in f, (name, lines[0])
    for reached in ("lock_wait", "config", "normalize", "prepare", "resolve", "enrich", "guard",
                    "fallback_clear", "record_override", "announce", "record_durable", "confirm"):
        assert f[f"{reached}_s"] != "-", (reached, lines[0])
    # Not reached in this harness: no picker listing, no cached agent, no session DB.
    assert f["listing_s"] == f["agent_switch_s"] == f["record_update_model_s"] == "-", lines[0]
    # The slow write-through is named twice: as its nested span and inside its phase.
    assert float(f["durable_set_override_s"]) >= 0.25, lines[0]
    assert float(f["record_durable_s"]) >= float(f["durable_set_override_s"]), lines[0]
    assert float(f["record_s"]) >= float(f["record_durable_s"]), lines[0]
    phases = sum(float(f[f"{n}_s"]) for n in mct.PHASES if f[f"{n}_s"] != "-")
    assert abs(phases + float(f["other_s"]) - float(f["total_s"])) < 0.01, lines[0]
    print("model_cmd line:", lines[0])


@pytest.mark.asyncio
async def test_lock_wait_measures_the_runner_wide_switch_lock(runner, caplog):
    lock = runner._model_switch_lock()
    await lock.acquire()  # a /model in another thread holds the runner-wide lock

    async def _release_later():
        await asyncio.sleep(0.3)
        lock.release()

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await asyncio.gather(_release_later(), runner._handle_model_command(_event("/model gpt-5.5")))
    f = _fields(_phase_lines(caplog)[0])
    assert float(f["lock_wait_s"]) >= 0.25 and float(f["lock_wait_s"]) < float(f["total_s"]), f


@pytest.mark.asyncio
async def test_handler_error_is_logged_as_error_and_propagates(runner, caplog, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("normalize failed")
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", _boom, raising=False)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        with pytest.raises(RuntimeError, match="normalize failed"):
            await runner._handle_model_command(_event("/model gpt-5.5"))
    f = _fields(_phase_lines(caplog)[0])
    assert f["outcome"] == "error" and f["normalize_s"] != "-" and f["resolve_s"] == "-", f
    assert not runner._model_switch_lock().locked(), "the switch lock must be released on error"


@pytest.mark.asyncio
async def test_broken_telemetry_never_changes_the_reply(runner, monkeypatch):
    def _broken(*_a, **_k):
        raise RuntimeError("telemetry broke")
    monkeypatch.setattr(mct.ModelCmdTiming, "line", _broken)
    monkeypatch.setattr(mct.ModelCmdTiming, "enter", _broken)
    reply = await runner._handle_model_command(_event("/model gpt-5.5"))
    assert reply and "gpt-5.5" in reply


def test_phase_and_span_outside_a_command_are_noops_and_finish_logs_once(caplog):
    mct._current.set(None)
    mct.phase("resolve")  # no timer: nothing to do, nothing raised
    with mct.span("durable_config"):
        pass
    timing = mct.begin()
    mct.phase("resolve")
    with pytest.raises(ValueError):
        with mct.span("durable_config"):
            raise ValueError("body exceptions propagate")
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        mct.finish(timing)
        mct.finish(timing)
    lines = _phase_lines(caplog)
    assert len(lines) == 1 and _fields(lines[0])["durable_config_s"] != "-", lines

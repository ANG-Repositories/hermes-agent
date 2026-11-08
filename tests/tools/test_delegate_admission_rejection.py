"""Interactive admission must never turn into a foreground child join."""

import contextvars
import json
import threading
from unittest.mock import Mock

import pytest

from gateway.session_context import restore_session_vars, set_session_vars
from tests.tools.test_delegate_apiserver_background import _fake_parent, _patch_delegate
from tools import async_delegation as ad


@pytest.fixture
def admission(monkeypatch):
    ad._reset_for_tests()
    dt = _patch_delegate(monkeypatch)
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.setattr("tools.async_delegation_kanban.ineligible_reason", lambda **kw: "test")
    tokens = set_session_vars(platform="telegram", chat_id="admission-test",
                              session_key="admission-test", session_id="parent-test",
                              async_delivery=True)
    release = threading.Event()
    entered = threading.Event()

    def sentinel(task_index, goal, **kwargs):
        entered.set()
        assert release.wait(10), "sentinel release timed out"
        return {"task_index": task_index, "status": "completed", "summary": "finished",
                "api_calls": 1, "duration_seconds": 0.0}

    monkeypatch.setattr(dt, "_run_single_child", sentinel)
    yield dt, release, entered
    release.set()
    ad._reset_for_tests()
    restore_session_vars(tokens)


@pytest.mark.parametrize("failure", ["durable_intent_unavailable", "registry_cap", "registry_error",
                                    "pool_capacity", "submission_failed"])
def test_interactive_admission_returns_without_executing_child(admission, monkeypatch, failure):
    dt, release, entered = admission
    if failure == "durable_intent_unavailable":
        monkeypatch.setattr(dt, "_build_durable_background_spec", Mock(side_effect=OSError("intent")))
    elif failure in {"registry_cap", "registry_error"}:
        monkeypatch.setattr(ad._store, "persist_dispatch", lambda **kw: (None, failure))
    elif failure == "pool_capacity":
        monkeypatch.setattr(dt, "_get_max_async_children", lambda: 0)
    else:
        executor = Mock()
        executor.submit.side_effect = RuntimeError("submission refused")
        monkeypatch.setattr(ad, "_get_executor", lambda *a: executor)
    done = threading.Event()
    result = {}
    parent = _fake_parent()

    def invoke():
        try:
            result["value"] = json.loads(dt.delegate_task(goal="sentinel", background=True, parent_agent=parent))
        except BaseException as exc:
            result["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=contextvars.copy_context().run, args=(invoke,), daemon=True)
    worker.start()
    try:
        assert done.wait(3), "interactive parent blocked on a rejected child"
        assert not entered.is_set(), "rejected child executed"
        assert "error" not in result, result
        value = result["value"]
        assert value["status"] == "rejected"
        assert value["reason"] == failure
        assert value["started"] is False
        assert value["retryable"] is True
        assert "delegation_id" not in value
        assert parent._active_children == []
        assert not ad._records
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()

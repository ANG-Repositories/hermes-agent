"""Parent routing is independent of boomerang context/cache eligibility."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import delegate_tool as dt


@pytest.mark.parametrize("mode", ["summary", "fork", "branch", None])
@pytest.mark.parametrize("explicit", [False, True])
def test_context_mode_does_not_select_route(monkeypatch, mode, explicit):
    chain = [{"provider": "openrouter", "model": "fallback-model"}]
    parent = SimpleNamespace(
        model="parent-model", provider="openrouter", requested_provider="openrouter",
        base_url="http://127.0.0.1:1/v1", api_key="synthetic-parent-key",
        api_mode="chat_completions", client=None, _client_kwargs={},
        _fallback_chain=copy.deepcopy(chain), request_overrides={"extra_body": {"nonce": "parent"}},
        _session_db=None, _delegate_depth=0, _active_children=[],
        _active_children_lock=threading.Lock(), _print_fn=None, tool_progress_callback=None,
        thinking_callback=None, prefill_messages=None, session_id="routing-parent",
        platform="cli", enabled_toolsets=[], disabled_toolsets=[],
        _session_messages=[{"role": "assistant", "content": "parent context"}],
    )
    captured = []

    def construct(**kwargs):
        captured.append(kwargs)
        child = MagicMock()
        child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1}
        child._delegate_depth = 1
        child.get_activity_summary.return_value = {"api_call_count": 1}
        return child

    monkeypatch.setattr("run_agent.AIAgent", construct)
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "worker-model", "fallback_providers": []})
    result = json.loads(dt.delegate_task(
        goal="read parent context", parent_agent=parent, inherit_mode=mode,
        model="explicit-child-model" if explicit else None,
    ))
    assert not result.get("error"), result
    assert captured
    expected = "explicit-child-model" if explicit else ("parent-model" if mode else "worker-model")
    assert captured[0]["model"] == expected
    assert (captured[0]["fallback_model"] or []) == (chain if mode and not explicit else [])
    assert parent._fallback_chain == chain

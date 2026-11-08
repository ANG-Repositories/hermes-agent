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
@pytest.mark.parametrize("disabled", [False, True])
def test_context_mode_does_not_select_route(monkeypatch, mode, explicit, disabled):
    chain = [] if disabled else [{"provider": "openrouter", "model": "fallback-model"},
                                {"provider": "openai", "model": "cross-vendor-model"}]
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
    if mode and not explicit:
        captured[0]["request_overrides"]["extra_body"]["nonce"] = "child"
        if chain:
            captured[0]["fallback_model"][0]["model"] = "child-model"
        assert parent.request_overrides["extra_body"]["nonce"] == "parent"
        assert parent._fallback_chain == chain


def test_route_snapshot_is_coherent_during_activation():
    from agent.route_snapshot import route_lock, snapshot_parent
    parent = SimpleNamespace(model="a", provider="a", base_url="http://a", api_key="a",
                             client=None, _client_kwargs={}, request_overrides={}, _fallback_chain=[])
    entered, release, captured = threading.Event(), threading.Event(), []

    def switch():
        with route_lock(parent):
            parent.provider = "b"
            entered.set()
            assert release.wait(5)
            parent.model, parent.base_url, parent.api_key = "b", "http://b", "b"

    writer = threading.Thread(target=switch)
    writer.start()
    assert entered.wait(5)
    reader = threading.Thread(target=lambda: captured.append(snapshot_parent(parent)))
    reader.start()
    release.set()
    writer.join(5)
    reader.join(5)
    assert not writer.is_alive() and not reader.is_alive()
    view = captured[0]
    assert (view.model, view.provider, view.base_url, view.api_key) == ("b", "b", "http://b", "b")


def test_recovered_routes_are_secret_free_and_session_isolated(monkeypatch):
    from tools.delegate_tool_routing import persist_route, recover_route_parent
    parent = SimpleNamespace(model="parent", provider="openrouter", api_mode="chat_completions",
                             base_url="http://parent", api_key="SECRET-SENTINEL", client=None,
                             _client_kwargs={}, _fallback_chain=[], request_overrides={})
    records = []
    for name in ("a", "b"):
        parent.requested_provider = name
        records.append(json.loads(json.dumps(persist_route(parent))))
    assert "SECRET-SENTINEL" not in json.dumps(records)
    calls = []

    def resolve(provider, **kw):
        calls.append(provider)
        return {"provider": provider, "base_url": "http://" + provider,
                "api_key": "SECRET-" + provider, "api_mode": "chat_completions"}

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", resolve)
    a = recover_route_parent(parent, records[0])
    b = recover_route_parent(parent, records[1])
    again = recover_route_parent(parent, records[0])
    assert calls == ["a", "b", "a"]
    assert (a.provider, b.provider, again.provider) == ("a", "b", "a")
    assert a.api_key == again.api_key != b.api_key
    assert a._fallback_chain == b._fallback_chain == []
    a.request_overrides["new"] = True
    assert not b.request_overrides and not parent.request_overrides
    with pytest.raises(ValueError, match="incomplete persisted identity"):
        recover_route_parent(parent, {"model": "missing-provider"})
    assert calls == ["a", "b", "a"]

"""Cold boomerang construction must use the foreground session route resolver."""
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as wake


def test_cold_parent_uses_session_identity_and_effective_policy(monkeypatch):
    seen = {}
    calls = []
    home = SimpleNamespace(session_id="pinned-parent", session_key="agent:main:discord:group:7", origin=object())
    chain = [{"provider": "synthetic-fallback", "model": "fallback-model"}]

    def resolve(**kw):
        calls.append(kw)
        return "pinned-model", {"provider": "synthetic-parent", "api_key": "synthetic-key", "base_url": "http://localhost:1"}

    runner = SimpleNamespace(
        session_store=SimpleNamespace(load_transcript=lambda sid: []),
        _resolve_session_agent_runtime=resolve, _refresh_fallback_model=lambda: chain,
        _pre_agent_fallback_notice="foreground-notice",
    )
    monkeypatch.setattr("run_agent.AIAgent", lambda **kw: (seen.update(kw) or SimpleNamespace()))
    wake._db_parent(runner, home)
    assert seen["model"] == "pinned-model"
    assert seen["fallback_model"] == chain
    assert calls[0]["session_key"] == home.session_key
    assert calls[0]["source"] is home.origin
    assert runner._pre_agent_fallback_notice == "foreground-notice"

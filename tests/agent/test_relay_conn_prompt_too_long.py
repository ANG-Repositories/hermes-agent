"""A relay-stamped ``conn`` on the bridge's "Prompt is too long" 500 is context overflow.

Agora, 2026-11-07 06:34-06:36 PST (session 20261106_095356_0af501bf): claude-bpr answered
``HTTP 500 {"error": {"message": "Claude Code returned an error result: Prompt is too long"}}``.
The claude-pool relay stamps ``x-relay-error-class: conn`` on every bridge 500 under
error-class-v2, and ``_relay_verdict`` ran before the text rules, so the deterministic
rejection classified as a transport timeout: the loop re-sent the byte-identical request
(c2u 2,641,478-2,641,500 bytes on every attempt) and never compressed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agent.error_classifier import FailoverReason, classify_api_error
from tests.run_agent.test_pool_capacity_503_retry import _make_agent, _response, _run

TOO_LONG = "Claude Code returned an error result: Prompt is too long"
CONN_HEADERS = {"x-relay-error-class": "conn", "x-relay-error-hop": "bridge->upstream",
                "x-relay-seat": "sub-vps-23", "x-relay-seats-tried": "1"}


class RelayError(Exception):
    """OpenAI-SDK-shaped error: ``body`` is the inner ``error`` object."""

    def __init__(self, status, message, headers):
        inner = {"message": message, "type": "internal_error"}
        super().__init__(f"Error code: {status} - {{'error': {inner!r}}}")
        self.status_code = status
        self.body = inner
        self.response = SimpleNamespace(status_code=status, headers=dict(headers),
                                        json=lambda: {"error": inner})


def test_relay_conn_on_prompt_too_long_is_context_overflow_but_real_conn_is_not():
    overflow = classify_api_error(RelayError(500, TOO_LONG, CONN_HEADERS), provider="claude-bpr",
                                  model="claude-opus-5-5", context_length=1_000_000)
    assert overflow.reason is FailoverReason.context_overflow
    assert overflow.should_compress is True

    # Same relay class on a genuine connect failure keeps its retry-in-place verdict.
    conn = classify_api_error(RelayError(503, "upstream connect timed out", CONN_HEADERS),
                              provider="claude-bpr", model="claude-opus-5-5", context_length=1_000_000)
    assert conn.reason is FailoverReason.timeout
    assert conn.should_compress is False


def test_loop_compresses_in_session_instead_of_replaying_the_rejected_request():
    statuses: list = []
    agent = _make_agent(statuses)
    agent.provider, agent.base_url = "claude-bpr", "http://127.0.0.1:18811/v1/"
    session_id = agent.session_id
    # Large enough that the request is not "unexplained" against the rig's 200k window.
    history = []
    for i in range(40):
        history += [{"role": "user", "content": f"q{i} " + "x" * 16_000},
                    {"role": "assistant", "content": f"a{i}"}]
    sent: list = []
    compress_calls: list = []

    def _compress(messages, system_message, **kwargs):
        compress_calls.append((len(messages), kwargs.get("trigger")))
        return [{"role": "user", "content": "[summary of earlier turns]"},
                {"role": "assistant", "content": "ok"}, messages[-1]], system_message

    err = RelayError(500, TOO_LONG, CONN_HEADERS)
    with patch.object(agent, "_compress_context", side_effect=_compress):
        result, activate, call = _run(agent, [err, _response("served")], [], )
        sent = [len(c.args[0].get("messages", [])) if c.args and isinstance(c.args[0], dict) else None
                for c in call.call_args_list]
    assert compress_calls and compress_calls[0][1] == "overflow"
    assert call.call_count == 2, sent          # one rejection, one retry on the compressed history
    assert result["completed"] is True and result["final_response"] == "served"
    activate.assert_not_called()               # same provider, no reset, no host move
    assert agent.session_id == session_id

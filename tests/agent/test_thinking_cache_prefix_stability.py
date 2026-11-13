"""A thinking tool loop on the a-family Claude lanes must read back its own conversation cache.

t_65e9cc2c: with ``preserve_thinking`` on and ``thinking_window="latest"`` (the lane property the pin
factory stamps on every a-family Anthropic-wire pin) the converter sends the latest assistant turn
WITH its signed thinking and the same turn WITHOUT it one request later. Both message breakpoints
sat on or after that turn, so request N+1 could read back only the system head: 1,336 of 1,336
thinking legs on three Studio apx units re-wrote the whole conversation (99.9% of missed tokens).

These tests build requests N and N+1 of a growing tool loop through the real cache planner, the
real lane resolution and the real Anthropic converter, and assert that N+1 repeats N's prompt
byte-for-byte up to the last message breakpoint N wrote, so N+1 reads that entry back.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

import agent.preserved_thinking as pt
from agent.anthropic_adapter import build_anthropic_kwargs
from agent.prompt_caching import build_prompt_cache_plan

STATIC = "static system prompt. " * 200
SYSTEM = STATIC + "volatile tail. " * 20
TOOLS = [{"type": "function", "function": {
    "name": "terminal", "description": "run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
}}]
LOOKBACK_BLOCKS = 20  # Anthropic checks for a cache hit at most 20 blocks before a breakpoint

# One test per lane kind: the alr face (local relay door) and a pinned apx seat unit.
LANES = {
    "claude-alr": "http://127.0.0.1:18810/anthropic",
    "claude-apx-9": "http://100.92.54.25:18809/anthropic",
}


@pytest.fixture
def lane_registry(monkeypatch):
    """The lane carries ``preserve_thinking`` (Ace 2026-10-17); no config key overrides it."""
    pt._logged_sources.clear()
    monkeypatch.setattr(pt, "_load_providers_config", lambda: {})
    import providers as reg
    profiles = {name: SimpleNamespace(api_mode="anthropic_messages", preserve_thinking=True,
                                      preserve_thinking_window="latest") for name in LANES}
    monkeypatch.setattr(reg, "get_provider_profile", lambda name: profiles.get(name))


def _transcript(steps: int, *, signed: bool = True) -> list:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "fix the failing test"}]
    for k in range(steps):
        thinking = {"type": "thinking", "thinking": f"step {k}: what to run next. " * 10}
        if signed:
            thinking["signature"] = f"sig-{k}-" + "x" * 40
        msgs.append({
            "role": "assistant", "content": "", "reasoning_details": [thinking],
            "tool_calls": [{"id": f"toolu_{k}", "type": "function",
                            "function": {"name": "terminal", "arguments": json.dumps({"command": f"ls {k}"})}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"toolu_{k}", "content": f"output of step {k}. " * 40})
    return msgs


def _request(provider: str, messages: list) -> dict:
    """The request the main loop sends: cache plan first, then the lane's Anthropic kwargs."""
    base_url = LANES[provider]
    enabled, window, source = pt.resolve_preserve_thinking(provider, base_url=base_url)
    assert (enabled, window, source) == (True, "latest", "lane")
    plan = build_prompt_cache_plan(copy.deepcopy(messages), TOOLS, cache_ttl="5m", native_anthropic=True,
                                   static_system_prefix=STATIC, direct_native_tool_cache=False)
    return build_anthropic_kwargs("claude-opus-4-8", plan.messages, plan.tools, 4096,
                                  {"enabled": True, "effort": "medium"}, base_url=base_url,
                                  preserve_thinking=enabled, thinking_window=window)


def _blocks(kwargs: dict) -> list:
    """``(serialized block without its marker, has_marker, is_message_block)`` in prompt order."""
    out = []
    for tool in kwargs.get("tools") or []:
        out.append((json.dumps({k: v for k, v in tool.items() if k != "cache_control"}, sort_keys=True),
                    "cache_control" in tool, False))
    system = kwargs.get("system")
    for block in system if isinstance(system, list) else [{"type": "text", "text": system}]:
        out.append((json.dumps({k: v for k, v in block.items() if k != "cache_control"}, sort_keys=True),
                    "cache_control" in block, False))
    for msg in kwargs["messages"]:
        content = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
        for block in content:
            body = {k: v for k, v in block.items() if k != "cache_control"}
            out.append((msg["role"] + ":" + json.dumps(body, sort_keys=True), "cache_control" in block, True))
    return out


def _read_back_breakpoint(prev: list, nxt: list) -> int:
    """Position of the last MESSAGE breakpoint request N wrote, asserted readable by N+1."""
    marks = [i for i, (_, marked, is_msg) in enumerate(prev) if marked and is_msg]
    assert marks, "request N carries no message breakpoint at all"
    pos = marks[-1]
    assert [b for b, _, _ in nxt[:pos + 1]] == [b for b, _, _ in prev[:pos + 1]], (
        f"N+1 does not repeat N's prompt up to N's last message breakpoint (block {pos}): "
        "the next request re-writes the conversation instead of reading it back")
    assert any(0 <= q - pos <= LOOKBACK_BLOCKS for q, (_, marked, _) in enumerate(nxt) if marked), (
        "no N+1 breakpoint within the lookback window reaches N's entry")
    return pos


@pytest.mark.parametrize("provider", sorted(LANES))
@pytest.mark.parametrize("signed", [True, False], ids=["signed", "unsigned"])
def test_thinking_tool_loop_reads_back_its_conversation(lane_registry, provider, signed):
    for steps in range(1, 8):
        prev = _blocks(_request(provider, _transcript(steps, signed=signed)))
        nxt = _blocks(_request(provider, _transcript(steps + 1, signed=signed)))
        pos = _read_back_breakpoint(prev, nxt)
        # The entry covers the conversation up to the thinking turn, not just the system head.
        message_blocks_before = sum(1 for _, _, is_msg in prev[:pos + 1] if is_msg)
        assert message_blocks_before >= 2 * steps - 1, (steps, message_blocks_before)


@pytest.mark.parametrize("provider", sorted(LANES))
def test_latest_turn_still_carries_its_signed_thinking(lane_registry, provider):
    """The fix moves breakpoints, never the thinking: Ace's 2026-10-17 lane ruling stands."""
    kwargs = _request(provider, _transcript(3))
    latest = [m for m in kwargs["messages"] if m["role"] == "assistant"][-1]
    assert latest["content"][0]["type"] == "thinking" and latest["content"][0]["signature"].startswith("sig-2-")
    markers = sum(1 for _, marked, _ in _blocks(kwargs) if marked)
    assert markers <= 4
    assert all("cache_control" not in b for b in latest["content"])

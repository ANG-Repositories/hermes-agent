"""Non-secret per-task boomerang route policy for existing delegation records."""
from __future__ import annotations

import copy

from tools.async_delegation_store import _carries_credential, persistable_fallback_chain


def persist_route(agent):
    captured = getattr(agent, "_boomerang_parent_route", None)
    route = copy.deepcopy(captured) if isinstance(captured, dict) else {"source": "parent", "model": agent.model,
             "provider": getattr(agent, "requested_provider", None) or agent.provider,
             "api_mode": agent.api_mode,
             "fallback_chain": copy.deepcopy(agent._fallback_chain),
             "request_overrides": copy.deepcopy(getattr(agent, "request_overrides", {}) or {})}
    original_chain = route["fallback_chain"] or []
    route["fallback_chain"] = persistable_fallback_chain(original_chain) or []
    if len(route["fallback_chain"]) != len(original_chain):
        raise ValueError("boomerang routing-unavailable: fallback credentials need symbolic references")
    # Headers may contain opaque secrets whose spelling no generic scrubber can recognize.
    overrides = route["request_overrides"]
    if overrides.get("extra_headers"):
        raise ValueError("boomerang routing-unavailable: request headers require live credential resolution")
    overrides.pop("extra_headers", None)
    if _carries_credential(route):
        raise ValueError("boomerang routing-unavailable: route policy contains credentials")
    return route


def recover_route_parent(parent, route):
    from agent.route_snapshot import snapshot_parent
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider

    if not isinstance(route, dict) or not route.get("provider") or not route.get("model"):
        raise ValueError("boomerang routing-unavailable: incomplete persisted identity")
    if _carries_credential(route) or "fallback_chain" not in route:
        raise ValueError("boomerang routing-unavailable: invalid persisted policy")
    runtime = _resolve_runtime_agent_kwargs_for_provider(route["provider"], target_model=route["model"])
    if not runtime.get("api_key") or not runtime.get("base_url"):
        raise ValueError("boomerang routing-unavailable: credentials could not be resolved")
    view = snapshot_parent(parent)
    view.model = route["model"]
    view.provider = runtime["provider"]
    view.requested_provider = route["provider"]
    view.base_url = runtime["base_url"]
    view.api_mode = runtime.get("api_mode") or route.get("api_mode")
    view.api_key = runtime["api_key"]
    view._client_kwargs = {"api_key": view.api_key, "base_url": view.base_url}
    view._fallback_chain = copy.deepcopy(route.get("fallback_chain") or [])
    view.request_overrides = copy.deepcopy(route.get("request_overrides") or {})
    return view

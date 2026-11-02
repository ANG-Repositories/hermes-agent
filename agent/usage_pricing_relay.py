"""Billing view of a multi-request relay usage payload (claude-bpx #564, t_9f190074).

The claude-bpx relay reports a turn that spanned several upstream requests as ONE usage object:
``prompt_tokens`` / ``prompt_tokens_details`` are the LAST request's own prompt (a context size,
never a sum), ``completion_tokens`` is the sum, ``upstream_requests`` = N and
``cumulative_prompt_tokens_details`` = the billed prompt sum split by field. ``normalize_usage``
keeps the last-request reading, which is what context consumers (compressor, anchor, meter) need;
a bill needs every request's prompt. This module derives the bill without touching that reading.

An arrival publish answers ``usage: null`` + ``unavailable`` + ``usage_deferred: true``: its tokens
are reported, exactly once, on the next response of the turn. Billing it as UNKNOWN marks a fully
measured turn unpriceable, and billing anything double counts, so it bills as a measured zero.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from agent.usage_pricing import CanonicalUsage

_DETAIL_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("cached_tokens", "cache_read_tokens"),
    ("cache_creation_tokens", "cache_write_tokens"),
)


def _relay_field(usage: Any, key: str) -> Any:
    """A relay-added usage field from a dict, attribute or pydantic extra."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    value = getattr(usage, key, None)
    if value is None:
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(key)
    return value


def _count(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not math.isfinite(raw) or raw < 0:
        return None
    return int(raw)


def usage_is_deferred(raw_usage: Any) -> bool:
    """True for an arrival publish whose usage rides with the next response of the turn."""
    return _relay_field(raw_usage, "usage_deferred") is True


def relay_billed_usage(raw_usage: Any, canonical: CanonicalUsage) -> CanonicalUsage:
    """The usage to BILL for one response; ``canonical`` itself when nothing differs.

    Multi-request (``upstream_requests`` > 1) with a complete ``cumulative_prompt_tokens_details``:
    the three prompt buckets come from that sum. A missing or partial split leaves the
    last-request buckets, so the call is under-billed as before and never guessed.
    """
    if usage_is_deferred(raw_usage):
        return CanonicalUsage(raw_usage=canonical.raw_usage)
    upstream = _count(_relay_field(raw_usage, "upstream_requests"))
    if upstream is None or upstream <= 1:
        return canonical
    details = _relay_field(raw_usage, "cumulative_prompt_tokens_details")
    if not isinstance(details, dict):
        return canonical
    buckets = {dst: _count(details.get(src)) for src, dst in _DETAIL_FIELDS}
    if any(v is None for v in buckets.values()):
        return canonical
    return replace(
        canonical,
        **buckets,
        input_tokens_unknown=False,
        cache_read_tokens_unknown=False,
        cache_write_tokens_unknown=False,
    )

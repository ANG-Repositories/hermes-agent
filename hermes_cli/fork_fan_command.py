"""Shared ``/fork-fan`` argument parsing and seed building (CLI + gateway).

``/fork-fan`` is sugar over the ``fork-fan`` skill (skills-shared/delegation/fork-fan, spec
plans/2026-11-06_fork-fan-spec.md): the message is rewritten into a skill invocation carrying the
parsed kind/board/items/override. It never calls delegate_task itself; the skill's procedure does
(plan -> one granted chunk per turn -> fold -> run).
"""
from __future__ import annotations

import json
import shlex
from typing import Callable, Dict, List, Optional, Tuple

USAGE = (
    "Usage: /fork-fan [--kind card|pr|path|text] [--board B] [--where k=v ...] "
    "[--model M --provider P] <items>\n"
    "Forks this session once per item (kanban cards by default, or PRs owner/repo#N, paths, free text; "
    "quote multi-word items). Each child inherits this conversation, works one item and writes a verdict; "
    "the verdicts fold back here as one table. --where selects cards instead of naming them. "
    "--model/--provider (both or neither) switch to summary mode: no cache-sharing fork, no sign grant."
)

KINDS = ("card", "pr", "path", "text")
_VALUE_FLAGS = ("--kind", "--board", "--model", "--provider")


def parse_fork_fan_args(raw: str) -> Tuple[Dict[str, object], List[str]]:
    """Return ``(opts, items)``; ``items`` may be empty (caller prints USAGE).

    ``opts`` keys: ``kind`` (default ``card``), ``board``, ``model``, ``provider`` (None when
    unset) and ``where`` (list of ``k=v`` selectors). Flags may appear anywhere; a bare ``--``
    ends flag parsing so later tokens are items even when they start with ``--``. Raises
    ``ValueError(USAGE)`` on an unknown flag, a missing/invalid value, unbalanced quotes, or
    ``--model`` without ``--provider`` (or the reverse). Nothing is silently dropped.
    """
    try:
        tokens = shlex.split(raw or "")
    except ValueError:
        raise ValueError(USAGE) from None
    opts: Dict[str, object] = {"kind": "card", "board": None, "model": None, "provider": None, "where": []}
    items: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            items.extend(tokens[i + 1:])
            break
        if not tok.startswith("--"):
            items.append(tok)
            i += 1
            continue
        name, eq, inline = tok.partition("=")
        if name not in _VALUE_FLAGS and name != "--where":
            raise ValueError(USAGE)
        if eq:
            value = inline
        elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            i += 1
            value = tokens[i]
        else:
            raise ValueError(USAGE)
        if not value.strip():
            raise ValueError(USAGE)
        if name == "--where":
            if "=" not in value:
                raise ValueError(USAGE)
            opts["where"].append(value)  # type: ignore[union-attr]
        elif name == "--kind":
            if value.lower() not in KINDS:
                raise ValueError(USAGE)
            opts["kind"] = value.lower()
        else:
            opts[name[2:]] = value
        i += 1
    if bool(opts["model"]) != bool(opts["provider"]):
        raise ValueError(USAGE)
    if opts["where"] and opts["kind"] != "card":
        raise ValueError(USAGE)
    return opts, items


def build_fork_fan_seed(
    opts: Dict[str, object], items: List[str],
    skill_message: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """The user-turn text that drives the fan. ``skill_message(instruction)`` renders the
    fork-fan skill invocation (None when the skill is not installed -> inline instruction).

    Items are carried verbatim as a JSON array (never spliced into a shell line); the skill
    hands them to ``fork_fan.py plan`` via ``--items-file`` or quoted argv.
    """
    lines = [f"kind: {opts.get('kind') or 'card'}"]
    if opts.get("board"):
        lines.append(f"board: {opts['board']}")
    if items:
        lines.append(f"items ({len(items)}, verbatim JSON array; pass via --items-file or quoted argv, "
                     f"never interpolate into a shell string): {json.dumps(items, ensure_ascii=False)}")
    for sel in opts.get("where") or []:
        lines.append(f"selector: --where {json.dumps(sel, ensure_ascii=False)}")
    if opts.get("model"):
        lines.append(f"override: summary mode, no cache-sharing fork "
                     f"(--model {json.dumps(opts['model'])} --provider {json.dumps(opts['provider'])}; no sign grant)")
    else:
        lines.append("override: none (fork mode; pin the fold to this session's live model/provider)")
    lines.append("Follow the fork-fan procedure: plan -> dispatch ONE granted chunk per turn (one "
                 "delegate_task call per chunk, copied from calls.json) -> fold -> run. Announce the cost "
                 "line before dispatching. Do not call delegate_task before plan has written calls.json.")
    instruction = "\n".join(lines)
    msg = skill_message(instruction) if skill_message else None
    if msg:
        return msg
    return (
        "Fork-fan this set using the fork-fan skill "
        "(~/.hermes/skills-shared/delegation/fork-fan/SKILL.md; load it with skill_view first).\n\n"
        + instruction
    )

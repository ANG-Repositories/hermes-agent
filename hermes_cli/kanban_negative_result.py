"""Negative-result detection for review handoffs (t_db9ca661).

Under ``kanban.review_policy=milestone_only|none`` a slice card's
``request_review`` completes the card in place. A handoff whose own summary
says the close gate is NOT met / not proven / FAIL must not become ``done``:
t_63322ecc, t_18004327 and t_96adeb3f were false greens Apollo had to reopen.

The pattern mirrors hermes-home ``scripts/kanban-review-merge-pass.py``
``NEG_RESULT_RE`` (the merge-pass half of the same fix, hermes-home#3574).
FAIL/DEFERRED count only as a capitalised verdict/result/gate VALUE: the bare
word matched 385 of 1392 completions in 14 days ("3 new tests fail on base").
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_NEG_ACCEPT = (
    r"\bacceptance\s+(?:is\s+|was\s+)?(?:still\s+)?unmet\b|\bnot\s+(?:yet\s+)?met\b"
    r"|\b(?:do\s+not|don'?t)\s+(?:merge|land)\b|\"acceptance_met\"\s*:\s*false"
)
NEG_RESULT_RE = re.compile(
    r"(?i:" + _NEG_ACCEPT + r"|\bnot\s+(?:yet\s+)?proven\b)|\bUNTRUSTED\b"
    r"|(?i:\b(?:verdict|result|gate))\s*[:=]?\s*(?:FAIL(?:ED)?|DEFERRED)\b"
)


def negative_result_match(summary: Optional[str], metadata: Any = None) -> Optional[str]:
    """The matched text when the handoff reports a negative result, else None.

    Scans ``summary`` plus the JSON form of ``metadata`` (the merge pass reads
    the stored ``task_runs.metadata`` text, so the two see the same bytes).
    """
    meta_text = ""
    if metadata:
        try:
            meta_text = json.dumps(metadata, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            meta_text = str(metadata)
    m = NEG_RESULT_RE.search((summary or "") + "\n" + meta_text)
    return m.group(0) if m else None


DRAFT_NOTE_PING_MAX = 300
_DRAFT_HEAD = "negative result: PR left as-is (a worker draft stays draft); "


def draft_cmds(metadata: Any = None) -> list:
    """``ace_gate.py ready <repo> <n>`` for each of the card's OWN fleet handoff PRs (t_5c6c67c5).

    A NOT-met handback leaves its PR exactly as it is: a worker draft stays draft, which keeps
    self-declared-unfinished work out of every lander until the human lane decides. Ownership is the
    freshness gate's own filter (:func:`kanban_pr_freshness.handoff_refs`): a PR the summary merely
    cites (another card's dependency) never becomes a command (Prism d0bec1125a49). No network read.
    """
    from hermes_cli.kanban_open_pr import split_fleet
    from hermes_cli.kanban_pr_freshness import handoff_refs

    meta = metadata if isinstance(metadata, dict) else None
    return [f"ace_gate.py ready {r.repo} {r.number}" for r in split_fleet(handoff_refs(meta))[0]]


def draft_note_fits(cmds: list, limit: int = DRAFT_NOTE_PING_MAX) -> bool:
    return len(_DRAFT_HEAD + "on accept flip it: " + "; ".join(cmds)) <= limit


def draft_note_line(cmds: list, limit: int = DRAFT_NOTE_PING_MAX) -> Optional[str]:
    """The review-ping line: every command whole, or a count pointing at the card comment. Never a
    sliced command, which could name a different valid PR (Prism 11a86869f533)."""
    if not cmds:
        return None
    if draft_note_fits(cmds, limit):
        return _DRAFT_HEAD + "on accept flip it: " + "; ".join(cmds)
    return _DRAFT_HEAD + f"{len(cmds)} flip commands, see the card"


def draft_note_comment(cmds: list) -> str:
    """Card comment carrying the full command list when the ping line only counts them."""
    return _DRAFT_HEAD + "on accept flip each one:\n" + "\n".join(cmds)

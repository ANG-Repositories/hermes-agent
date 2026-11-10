"""Entry validation and durable notices for bare Studio pins."""
from __future__ import annotations

import logging
import re
import sys

from hermes_cli import kanban_worker_pool as pool

_FIRST_LINE = re.compile(r"^[ \t]*host:[ \t]*(?:studio|mac-studio[\w-]*)[ \t]*$", re.IGNORECASE)
_STUDIO_NEEDS = frozenset({"studio_relay", "studio_launchd", "vault", "skills_tree", "studio_tree"})
NOTICE_PREFIX = "[placement] bare host: studio dropped: add requires: "
_log = logging.getLogger(__name__)


def prepare_body(body, notices=None):
    """Validate against the dispatcher's vocabulary; unknown maps retain the existing safe-local behavior."""
    # Saved bodies carry provenance before the caller's placement directives.
    content = pool.without_header_lines(body).lstrip("\r\n")
    if not content or not _FIRST_LINE.fullmatch(content.splitlines()[0]):
        return body
    from hermes_cli.config import load_config
    mode = load_config().get("kanban", {}).get("studio_pin_requires_reason", "notice")
    if mode is False or mode == "off":
        return body
    from hermes_cli import kanban_db as kb, kanban_host_capabilities as caps
    reasons = pool.studio_map_reasons(caps.load_map(kb.kanban_home() / "fleet"))
    if reasons is None or pool.studio_pin_justified(body, reasons):
        return body
    if pool.card_needs(body) & _STUDIO_NEEDS and "studio-state" in reasons:
        # One line, including when the caller already declared unrelated requirements.
        head = re.compile(r"^([ \t]*requires:[ \t]*)", re.IGNORECASE | re.MULTILINE)
        if head.search(body):
            return head.sub(lambda m: m.group(1) + "studio-state, ", body, count=1)
        # After the host directive itself: a provenance header may precede it on a stored body.
        lines = body.splitlines(keepends=True)
        at = next(i for i, ln in enumerate(lines) if _FIRST_LINE.fullmatch(ln.rstrip("\r\n")))
        host = lines[at] if lines[at].endswith("\n") else lines[at] + "\n"
        return "".join(lines[:at]) + host + "requires: studio-state" + ("\n" if at + 1 < len(lines) else "") \
            + "".join(lines[at + 1:])
    if mode is not True and mode != "refuse":
        if notices is not None:
            notices.append(reasons)
        return body
    raise ValueError(
        "bare host: studio requires a Studio-served reason; accepted words: "
        + ", ".join(sorted(reasons))
        + ". Add a line `requires: <word>` or remove the host: studio line."
    )


def report_notices(conn, card_id, notices):
    """Report only after a successful write, sharing dispatch's durable dedup key."""
    for reasons in notices:
        try:
            note_drop(conn, card_id, reasons)
        except Exception as exc:  # the card is already saved; the dispatcher posts the notice later
            _log.warning("placement notice for %s not written: %s", card_id, exc)
        print("Warning: bare host: studio accepted without a Studio-served reason; "
              "accepted words: " + ", ".join(sorted(reasons))
              + ". Add `requires: <word>` or remove the host: studio line.", file=sys.stderr)


def note_drop(conn, card_id, reasons):
    """Persist one notice per card, not one per dispatcher process or retry."""
    from hermes_cli import kanban_db as kb
    exists = ("SELECT 1 FROM task_comments WHERE task_id=? AND substr(body,1,?)=? LIMIT 1",
              (card_id, len(NOTICE_PREFIX), NOTICE_PREFIX))
    # Dispatch re-routes dropped pins every tick: an existing notice must not cost the board's write lock.
    if conn.execute(*exists).fetchone():
        return
    with kb.write_txn(conn):
        if conn.execute(*exists).fetchone():
            return
        kb.add_comment(conn, card_id, "dispatcher",
                       NOTICE_PREFIX + ", ".join(sorted(reasons or ())))

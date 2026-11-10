"""Run the fleet-owned citation predicate before core kanban writes.

The checker ships in hermes-home, not upstream Hermes. A missing checker is
inert; a present but broken checker refuses writes rather than silently
allowing uncited attributions. Deploy the checker before this runtime seat.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _fleet_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def validate_fields(conn, fields) -> None:
    if os.environ.get("RULING_CITATION_LINT", "").strip().lower() in ("0", "off", "false", "no"):
        return
    fields = [(name, text) for name, text in fields if text]
    if not fields:
        return
    path = _fleet_root() / "scripts/lints/ruling-citation-lint.py"
    if not path.is_file():
        return
    try:
        spec = importlib.util.spec_from_file_location("_kanban_ruling_citation", path)
        if spec is None or spec.loader is None:
            raise ImportError("no citation checker loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def comment_cites(cid):
            row = conn.execute("SELECT body FROM task_comments WHERE id=?", (cid,)).fetchone()
            return bool(row) and module.direct_citation(row[0] or "")

        reason = module.check_fields(fields, comment_cites)
    except Exception as exc:
        raise ValueError(
            "BLOCKED by ruling-citation: checker unavailable; repair the checker "
            "or set RULING_CITATION_LINT=0."
        ) from exc
    if reason:
        raise ValueError(reason)

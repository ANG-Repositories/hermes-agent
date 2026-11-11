"""Byte copies of hermes-home ``scripts/lib/{placement,admission,work_estimate,admission_demand,alloc_client}.py``
(t_29403e19).

The fleet has ONE placement scorer and ONE admission predicate; the fork may not import hermes-home code, so it
carries copies. ``VENDORED.json`` pins the source ref and each file's sha256, and
``tests/hermes_cli/test_fleet_placement_vendored.py`` fails when a copy's digest differs from the pin or the pin is
older than ``max_age_days``. Re-vendor by copying the files byte for byte and updating ``VENDORED.json``; never edit
the copies here. Every fork-side difference lives in this adapter.

The one difference: the state root. hermes-home resolves ``PLACEMENT_HOME`` else the checkout two levels above
``scripts/lib``; here the root is the shared kanban root (``<root>/config/fleet-hosts.json``, ``<root>/var/placement``),
resolved at call time so a test's ``HERMES_KANBAN_HOME`` binds it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import admission, placement, work_estimate

__all__ = ["admission", "admission_demand", "placement", "work_estimate"]


def __getattr__(name):
    # admission_demand imports fcntl/pwd at module top (POSIX-only); loading it eagerly made
    # `import gateway.run` fail on native Windows. It loads on first use instead, root bound here.
    if name != "admission_demand":
        raise AttributeError(name)
    import importlib

    mod = importlib.import_module(f"{__name__}.admission_demand")
    mod._root = _demand_root
    return mod


def _fleet_root() -> Path:
    env = os.environ.get("PLACEMENT_HOME")
    if env:
        return Path(env)
    from hermes_cli import kanban_db

    return kanban_db.kanban_home()


def _demand_root(root=None) -> Path:
    return Path(root) if root is not None else placement.fleet_root()


placement.fleet_root = _fleet_root


# Second difference (t_1cfdbd7b, 2026-11-10): the pinned placement.validate() rejects an `unmeasurable_axes` name it
# does not know. The fleet's host table gained `psi_io_some10` for its Darwin hosts, and that one name made the
# WHOLE table unreadable here: `kanban admit --host` refused every pool host and the gateway's demand publish failed
# on every tick. An exemption for an axis this reader never requires is a no-op, so validation sees each row without
# the names the pinned admission.DARWIN_UNMEASURABLE already declares and this reader's REQUIRED_AXES lacks. The row
# itself is untouched (load_hosts keeps the name, and admission is told the host cannot report it). Any other
# unknown name, and a non-list value, are rejected exactly as before. Goes away with the copies.
_pinned_validate = placement.validate


def _validate(doc):
    hosts = doc.get("hosts") if isinstance(doc, dict) else None
    if isinstance(hosts, dict):
        tolerated = set(admission.DARWIN_UNMEASURABLE) - set(placement.REQUIRED_AXES)
        seen = {}
        for hid, row in hosts.items():
            axes = row.get("unmeasurable_axes") if isinstance(row, dict) else None
            if isinstance(axes, list) and any(isinstance(a, str) and a in tolerated for a in axes):
                row = dict(row, unmeasurable_axes=[a for a in axes if not (isinstance(a, str) and a in tolerated)])
            seen[hid] = row
        doc = dict(doc, hosts=seen)
    return _pinned_validate(doc)


placement.validate = _validate

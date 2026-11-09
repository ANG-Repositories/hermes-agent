"""Test fixture: write a class demand file (var/placement/demand/<workload>.json) in the READER's format.

Mirrors hermes-home scripts/tests/demand_fixture.py (#9223). The production writer is allocd's pair door
(admission_demand.store_door, reached through alloc_client); the local admission_demand.publish path was deleted
(t_810c9012 round 4). Reader tests only need the file shape, so they seed it here. Never imported by production.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA = 1
DEMAND_REL = Path("var") / "placement" / "demand"


def write_demand(workload, queued, unit, *, root, hosts=None, now=None, env=None):
    """``unit`` = admission.Estimate. ``env`` sets ``unified`` exactly as the publisher's kill switch would."""
    env = os.environ if env is None else env
    key = workload.upper().replace("-", "_") + "_UNIFIED_ADMIT"
    doc = {"schema": SCHEMA, "workload": workload, "at": float(time.time() if now is None else now),
           "queued": max(0, int(queued)), "hosts": None if hosts is None else sorted(str(h) for h in hosts),
           "unit": {"cpus": float(unit.cpus), "ram_gb": float(unit.ram_gb), "gpu_mib": float(unit.gpu_mib)},
           "unified": str(env.get(key, "1")).strip() != "0"}
    d = Path(root) / DEMAND_REL
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{workload}.json"
    path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    return path

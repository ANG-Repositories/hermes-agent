# SCHEDULE: manual (library imported by each class's own scheduler; nothing runs this file on a cadence)
"""admission_demand: the I/O half of the ONE admission rule (t_e5124426). admission.admit_class() is pure; this module
reads its two inputs from disk and gives every caller the same kill switch.

  load_priority(path=None) -> admission.Priority     fleet/admission-priority.json (order, line, holdback_units)
  store_door(req, *, owner, root=None, now=None)    the ONE writer of var/placement/demand/<workload>.json, called
                                                     only by allocd's POST /demand/publish (alloc_client is the client:
                                                     enqueue / withdraw / mark_asap / publish_queue). There is no local
                                                     publish path (t_810c9012 round 4 cut): one owner model, the pair.
  read_demand(host, root=None, now=None, resolve=None) -> [admission.Demand]
                                                     every class's FRESH queue that may run on ``host``
  unified(workload, env=None) -> bool                the per-caller kill switch: <WORKLOAD>_UNIFIED_ADMIT=0 = the
                                                     caller's old local rule (one release, card step 4)
  published_unified(workload, root=None, now=None) -> bool | None
                                                     that switch as the class's admitter published it (demand file
                                                     ``unified``), for a reader in another process

Demand file (one per class, atomic replace; the class's own scheduler writes it every tick):
  {"schema": 1, "workload": "prism", "at": <epoch>, "queued": 64, "hosts": ["ci-box", "ace-ai"] | null,
   "unit": {"cpus": 1.0, "ram_gb": 4.0, "gpu_mib": 0}, "unified": true}
``unified`` = the publisher's <WORKLOAD>_UNIFIED_ADMIT switch (absent in a file from an older publisher).
``hosts`` null = may run anywhere. A file older than demand_max_age_s, unreadable, or malformed holds nothing back
(said once on stderr): a dead publisher must never starve the other classes, and the host line still protects the box.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import pwd
import re
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Mapping, Optional

try:
    from . import admission
except ImportError:  # scripts/lib on sys.path
    import admission

SCHEMA = 1
PRIORITY_REL = Path("fleet") / "admission-priority.json"
DEMAND_REL = Path("var") / "placement" / "demand"
DEFAULT_MAX_AGE_S = 120.0
_SAID: set = set()
_PROVEN_DIRS: dict = {}     # demand dir -> (st_dev, st_ino) whose whole chain from the store root THIS process fsync'd
_UNPROVEN_TOP: dict = {}    # demand dir -> top of a chain whose sync failed after mkdir (retry walks up to it)


def _root(root=None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("PLACEMENT_HOME")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def _say(key: str, msg: str) -> None:
    if key not in _SAID:
        _SAID.add(key)
        sys.stderr.write(f"admission_demand: {msg}\n")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _read_policy(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _say(f"policy:{path}", f"{path} unreadable ({exc}); built-in priority defaults")
        return {}
    return doc if isinstance(doc, dict) else {}


def validate_priority(doc: Mapping) -> List[str]:
    errs = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    order = doc.get("order")
    if not (isinstance(order, list) and order and all(isinstance(x, str) and x for x in order)
            and len(set(order)) == len(order)):
        errs.append("order must be a non-empty list of distinct class names")
    line = doc.get("line")
    if not (_num(line) and 0 < line <= admission.HOST_LINE):
        errs.append(f"line must be a number in (0, {admission.HOST_LINE}] (Ace: no host above 90 % busy)")
    hb = doc.get("holdback_units")
    if not (isinstance(hb, int) and not isinstance(hb, bool) and 0 <= hb <= 64):
        errs.append("holdback_units must be an int in [0, 64]")
    age = doc.get("demand_max_age_s")
    if not (_num(age) and age > 0):
        errs.append("demand_max_age_s must be a number > 0")
    return errs


def priority_from_doc(doc: Mapping) -> admission.Priority:
    """Normalize independently: one bad field never discards a valid line/order."""
    defaults = admission.Priority()
    # Only a foreign schema discards the file. Every field, legacy or additive, degrades on its own:
    # one bad holdback_units never resets a valid line/order/priority_mode (round 2, finding 4).
    if doc.get("schema", SCHEMA) != SCHEMA:
        _say(f"schema:{doc.get('schema')!r}", f"priority schema must be {SCHEMA}; built-in defaults")
        return defaults
    values = {}
    checks = {
        "order": lambda v: isinstance(v, (list, tuple)) and bool(v) and all(isinstance(x, str) and x for x in v)
                 and len(set(v)) == len(v),
        "line": lambda v: _num(v) and 0 < v <= admission.HOST_LINE,
        "holdback_units": lambda v: type(v) is int and 0 <= v <= 64,
        "demand_max_age_s": lambda v: _num(v) and v > 0,
        "priority_mode": lambda v: v in ("spawn-only", "fcfs", "classes"),
        "door_window_units": lambda v: type(v) is int and 1 <= v <= 64,
        "door_hold_max_s": lambda v: _num(v) and v > 0,
        "verdict_trust_cpu": lambda v: _num(v) and v >= 0,
        "asap_max_units_per_host": lambda v: type(v) is int and 0 <= v <= 64,
        "preempt_budget_per_tick": lambda v: type(v) is int and v == 0,
    }
    for name, check in checks.items():
        value = doc.get(name, getattr(defaults, name))
        if not check(value):
            _say(f"field:{name}:{value!r}", f"invalid {name}; using default (preempt not implemented)" if
                 name == "preempt_budget_per_tick" else f"invalid {name}; using default")
            value = getattr(defaults, name)
        values[name] = tuple(value) if name == "order" else value
    classes = doc.get("classes", {})
    values["classes"] = {}
    if isinstance(classes, dict):
        for name, fields in classes.items():
            if not isinstance(fields, dict):
                continue
            values["classes"][name] = {
                "footprint_class": fields.get("footprint_class", name) if isinstance(fields.get("footprint_class", name), str) else name,
                "age_step_s": fields.get("age_step_s", 1800) if fields.get("age_step_s", 1800) in (0, 1800) else 1800,
                "yield_on": fields.get("yield_on", "start") if fields.get("yield_on", "start") in ("start", "growth") else "start"}
    return admission.Priority(**values)


def load_priority(path=None, root=None) -> admission.Priority:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    return priority_from_doc(_read_policy(p))


def priority_document(*, root=None) -> dict:
    p = _root(root) / PRIORITY_REL
    try:
        raw = p.read_bytes()
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        raw, doc = b"", {}
    return {"priority": dataclasses.asdict(priority_from_doc(doc)),
            "priority_sha256": hashlib.sha256(raw).hexdigest()}


def max_age_s(path=None, root=None) -> float:
    p = Path(path) if path is not None else _root(root) / PRIORITY_REL
    doc = _read_policy(p)
    v = doc.get("demand_max_age_s") if doc else None
    return float(v) if _num(v) and v > 0 else DEFAULT_MAX_AGE_S


def _name(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value) is not None


def make_dirs_durable(directory, *, mode=0o777, through=None) -> None:
    """``mkdir -p`` whose NEW entries survive a power loss (Prism 570db39355af; shared with alloc_client).

    A created directory is durable only once its entry in the parent is, so after the mkdir every created level AND
    the deepest pre-existing ancestor (whose entry list changed) is fsync'd, and nothing above it. ``through`` names
    an ancestor that is always synced down from, created or not (alloc_client's fleet root). An existing chain with
    no ``through`` syncs nothing here; the caller's own leaf fsync covers its file. OSError propagates."""
    directory = Path(directory)
    anchor = directory
    while not anchor.exists() and anchor.parent != anchor:
        anchor = anchor.parent
    directory.mkdir(parents=True, exist_ok=True, mode=mode)
    top = anchor if through is None or anchor in Path(through).parents else Path(through)
    if top == directory:
        return   # leaf not fsync'd here: correct only because every caller fsyncs the leaf after its write (_replace)
    for path in [directory] + [p for p in directory.parents if p == top or top in p.parents]:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def _writer(root=None):
    base = _root(root)
    directory = base / DEMAND_REL
    # before the first record (or tombstone) can be acknowledged. Existence is not durability (Prism cdecee16d1ed): a
    # chain left by a failed sync, here or in a crashed process, is re-synced from the store root down until this
    # process has PROVEN it; only then do later writes skip the walk and rely on _replace's leaf fsync. When mkdir
    # creates the store root itself, the pre-mkdir anchor above it is kept until success, so a retry after a failed
    # ancestor sync walks up to that anchor again rather than stopping at the (now existing) root.
    # Proof is the dir's (st_dev, st_ino), not its path: a dir deleted and recreated between writes is a new inode
    # whose parent entry was never synced (reader deleg_29a9bec5). One stat per write, replacing is_dir().
    key = str(directory)
    try:
        st = os.stat(directory)
        ident = (st.st_dev, st.st_ino)
    except OSError:
        ident = None
    if ident is None or _PROVEN_DIRS.get(key) != ident:
        _PROVEN_DIRS.pop(key, None)   # a vanished dir's old proof is void until this re-prove succeeds (Prism 324248e82c6f)
        top = _UNPROVEN_TOP.get(key)
        if top is None:
            anchor = directory
            while not anchor.exists() and anchor.parent != anchor:
                anchor = anchor.parent
            top = _UNPROVEN_TOP[key] = anchor if anchor in base.parents else base
        make_dirs_durable(directory, mode=0o755, through=top)
        st = os.stat(directory)
        _UNPROVEN_TOP.pop(key, None)
        _PROVEN_DIRS[key] = (st.st_dev, st.st_ino)
    with (directory / ".writer.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield directory


def _replace(path, doc):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".demand.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # The rename is durable only once the directory is: a spend intent lost to a crash here would make a consumed
    # id eligible again. Same rule as placement._write_ledger; a failed directory fsync raises (OSError -> 503).
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _owned(path, workload, root=None):
    """All merged files belong to allocd's service UID; publisher identity is checked before merging.

    owners maps each kind to its registered filesystem service user (or numeric UID).
    Missing policy retains same-UID compatibility, never trusts a different filesystem owner.
    """
    uid = _owner_uid(workload, root)
    try:
        return uid is not None and path.stat().st_uid == uid and not path.is_symlink()
    except OSError:
        return False


def _owner_uid(workload, root=None):
    """The configured filesystem owner of ``workload``'s store (UID), None when the policy names no resolvable one."""
    owners = _read_policy(_root(root) / PRIORITY_REL).get("owners", {})
    owner = owners.get(workload, os.getuid()) if isinstance(owners, dict) else os.getuid()
    try:
        return owner if type(owner) is int else pwd.getpwnam(owner).pw_uid
    except (KeyError, TypeError):
        return None


class DemandStoreUnreadable(OSError):
    """An owned demand store exists but cannot be read/parsed. It carries the consumed tombstones (anti-replay), so
    every writer refuses to replace it (allocd answers 503) instead of treating it as empty (Prism e478e39ea54b)."""


def _existing(path, workload, root=None):
    absent = not path.exists() and not path.is_symlink()
    # allocd is the single writer, and _replace always leaves a file owned by THIS process's UID. If the policy names
    # another owner, read_demand would hide whatever we write, so EVERY write is refused before any mutation, whether
    # the store is absent or already exists (never accept-then-hide; t_430834ff). An existing store must also carry
    # the configured owner.
    if _owner_uid(workload, root) != os.getuid() or (not absent and not _owned(path, workload, root)):
        # Never erase queues or anti-replay tombstones because ownership policy changed.
        from episode_pager import EpisodePager
        try:
            outcome = EpisodePager("admission-demand", state_dir=_root(root) / "state/pager").page(
                f"unowned:{workload}", f"Admission store ownership refused: {path}; no writes permitted")
        except Exception as exc:  # paging failure must not weaken the storage refusal
            raise DemandStoreUnreadable(f"ownership refusal page failed for {path}: {exc}") from exc
        if outcome == "failed":
            raise DemandStoreUnreadable(f"ownership refusal page failed for {path}; retry next request")
        raise DoorConflict("auth")
    if absent:
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemandStoreUnreadable(f"{path}: unreadable demand store ({exc}); refusing to replace it") from exc
    if not isinstance(doc, dict) or doc.get("workload") != workload:
        raise DemandStoreUnreadable(f"{path}: malformed demand store; refusing to replace it")
    return doc


class DoorConflict(Exception):
    """A well-formed door operation the current queue state refuses (HTTP 409 with ``reason``)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_door(req, registry=None):
    """Shared remote wire schema. No client clock or remote urgency is authoritative."""
    registry = registry or admission.Priority()
    if not isinstance(req, dict) or req.get("schema") != SCHEMA:
        raise ValueError("schema:1 required")
    if not _name(req.get("workload")) or not _name(req.get("host")):
        raise ValueError("workload and canonical host required")
    op = req.get("op", "publish")
    if op not in ("publish", "enqueue", "withdraw", "mark_asap", "queue"):
        raise ValueError("unknown demand operation")
    if op == "queue":
        # The class's aggregate queue (legacy holdback input). The server stamps ``at``; hosts is the pair's host.
        q, unit = req.get("queued"), req.get("unit")
        if type(q) is not int or q < 0 or type(req.get("unified")) is not bool or not isinstance(unit, dict):
            raise ValueError("queue requires queued:int>=0, unified:bool, unit")
        if not all(_num(unit.get(k)) and unit[k] >= 0 for k in ("cpus", "ram_gb", "gpu_mib")) or "at" in req:
            raise ValueError("invalid queue unit or client timestamp")
        return req
    if op in ("enqueue", "withdraw", "mark_asap"):
        if not isinstance(req.get("id"), str) or not 0 < len(req["id"]) <= 256:
            raise ValueError("stable pending-unit id required")
        if op in ("withdraw", "mark_asap"):
            return req
    waiting = ([{"id": req["id"], "need_cpu": req.get("need_cpu"),
                 "need_mem_gb": req.get("need_mem_gb"), "asap": req.get("asap", False),
                 **({"at": req["at"]} if "at" in req else {})}] if op == "enqueue" else req.get("waiting"))
    verdict = req.get("verdict")
    if not isinstance(waiting, list) or len(waiting) > registry.door_window_units:
        raise ValueError("waiting exceeds door window")
    ids = set()
    for unit in waiting:
        if not isinstance(unit, dict) or not isinstance(unit.get("id"), str) or not 0 < len(unit["id"]) <= 256:
            raise ValueError("stable pending-unit id required")
        if unit["id"] in ids:
            raise ValueError("duplicate pending-unit id")
        ids.add(unit["id"])
        if unit.get("asap") is not False:
            raise ValueError("remote ASAP forbidden")
        if not (_num(unit.get("need_cpu")) and unit["need_cpu"] > 0
                and _num(unit.get("need_mem_gb")) and unit["need_mem_gb"] >= 0):
            raise ValueError("invalid waiting footprint")
        if "at" in unit:
            raise ValueError("client arrival timestamps forbidden")
    if op == "enqueue" and verdict is None:
        return req
    if not isinstance(verdict, dict) or type(verdict.get("admittable")) is not bool:
        raise ValueError("boolean admittable required")
    if not isinstance(verdict.get("binding"), str) or len(verdict["binding"]) > 128:
        raise ValueError("binding required")
    if not _num(verdict.get("deficit_cpu")) or verdict["deficit_cpu"] < 0 or "at" in verdict:
        raise ValueError("invalid verdict deficit or timestamp")
    return req


def consumed_units(doc):
    """Workload-wide spend index, including legacy per-host tombstones (never freshness-gated)."""
    consumed = dict(doc.get("consumed", {}))
    for state in doc.get("by_host", {}).values():
        for unit_id, value in state.get("consumed", {}).items():
            consumed.setdefault(unit_id, value)
    return consumed


def store_door(req, *, owner: str, root=None, now=None):
    """Atomic per-unit merge; the server owns every clock and every owner.

    ``owner`` is the AUTHENTICATED pair identity (``workload@host``) allocd checked before calling. ONE owner per
    unit id (t_810c9012 round 4): the pair that enqueued a live id owns it; enqueue / publish-refresh / withdraw /
    mark_asap of a live id owned by another pair raises DoorConflict("auth") and changes nothing. A unit gone stale
    (not re-asserted for demand_max_age_s) is gone: a later enqueue by any pair is a NEW record with a NEW
    server-stamped arrival. A caller-supplied ``at`` is refused by validate_door.
    """
    if not isinstance(owner, str) or owner.count("@") != 1 or not all(map(_name, owner.split("@"))):
        raise ValueError("authenticated owner identity required")
    now = float(time.time() if now is None else now)
    registry = load_priority(root=root)
    validate_door(req, registry)
    workload, host = req["workload"], req["host"]
    with _writer(root) as directory:
        path = directory / f"{workload}.json"
        doc = _existing(path, workload, root)
        if req.get("op") == "queue":
            # Class-level aggregate (legacy holdback input), last writer wins; ``hosts`` keeps every pair host that
            # published within demand_max_age_s, so two hosts' publishers never hide each other's restriction.
            # Each host keeps ITS OWN publication time (``host_at``): one live publisher never keeps a stopped
            # publisher's host alive (t_430834ff). A legacy doc without host_at dates its hosts by the doc ``at``.
            host_at = doc.get("host_at")
            if not isinstance(host_at, dict):
                host_at = ({str(h): doc["at"] for h in doc["hosts"]} if _num(doc.get("at"))
                           and isinstance(doc.get("hosts"), list) else {})
            host_at = {str(h): t for h, t in host_at.items()
                       if _num(t) and 0 <= now - t <= registry.demand_max_age_s}
            host_at[host] = now
            doc.update(schema=SCHEMA, workload=workload, at=now, queued=req["queued"],
                       hosts=sorted(host_at), host_at=host_at,
                       unit={k: float(req["unit"][k]) for k in ("cpus", "ram_gb", "gpu_mib")},
                       unified=req["unified"])
            _replace(path, doc)
            return {"at": now, "queued": req["queued"]}
        by_host = doc.get("by_host", {})
        if not isinstance(by_host, dict):
            by_host = {}
        previous = by_host.get(host, {})
        fresh = _num(previous.get("at")) and 0 <= now - previous["at"] <= registry.demand_max_age_s
        # Live units only: a stale unit is dropped here, so its id re-enqueues as a NEW record (new arrival, new owner).
        waiting = {u["id"]: u for u in previous.get("waiting", [])
                   if isinstance(u, dict) and isinstance(u.get("id"), str) and _num(u.get("at"))
                   and fresh and _num(u.get("seen_at", previous.get("at")))
                   and 0 <= now - u.get("seen_at", previous["at"]) <= registry.demand_max_age_s}
        op = req.get("op", "publish")
        consumed = consumed_units(doc)
        touched = [req["id"]] if op in ("enqueue", "withdraw", "mark_asap") else [u["id"] for u in req["waiting"]]
        if op in ("enqueue", "publish"):
            for other_host, state in by_host.items():
                if other_host == host or not _num(state.get("at")) or not 0 <= now - state["at"] <= registry.demand_max_age_s:
                    continue
                if any(u.get("id") in touched and u.get("id") not in consumed
                       and _num(u.get("seen_at", state["at"]))
                       and 0 <= now - u.get("seen_at", state["at"]) <= registry.demand_max_age_s
                       for u in state.get("waiting", [])):
                    raise DoorConflict("invalid")  # re-placement requires withdraw on the old host first
        if any(i in waiting and waiting[i].get("owner") != owner for i in touched):
            raise DoorConflict("auth")       # another pair's live unit: never honoured, never silently skipped
        if op == "withdraw":
            waiting.pop(req["id"], None)
        elif op == "mark_asap":
            # Operator-only verb (authenticated by allocd): urgency on an EXISTING unit, arrival untouched.
            unit = waiting.get(req["id"])
            if unit is None or req["id"] in consumed:
                raise DoorConflict("not_enqueued")
            if not unit.get("asap") and sum(1 for u in waiting.values()
                                            if u.get("asap") is True) >= registry.asap_max_units_per_host:
                raise DoorConflict("asap_cap")
            waiting[req["id"]] = {**unit, "asap": True}
        else:
            incoming = ([{"id": req["id"], "need_cpu": req["need_cpu"],
                          "need_mem_gb": req["need_mem_gb"]}] if op == "enqueue" else req["waiting"])
            for u in incoming:
                # A re-assertion keeps first-seen arrival and any operator urgency; it never grants urgency.
                known = waiting.get(u["id"], {})
                waiting[u["id"]] = {"id": u["id"], "owner": owner, "at": min(known.get("at", now), now),
                    "seen_at": now, "need_cpu": u["need_cpu"], "need_mem_gb": u["need_mem_gb"],
                    "asap": known.get("asap") is True}
        consumed = {key: value for key, value in consumed.items()
                    if isinstance(value, dict) and _num(value.get("at")) and now - value["at"] <= 7 * 86400}
        waiting = [u for uid, u in waiting.items() if uid not in consumed]
        verdict = ({**req["verdict"], "at": now} if req.get("verdict") is not None else previous.get("verdict"))
        if waiting and (not isinstance(verdict, dict) or not _num(verdict.get("at"))
                        or not 0 <= now - verdict["at"] <= registry.demand_max_age_s):
            # No fresh capacity fact: conservatively retain strict ordering, but do
            # not fabricate positive headroom or a numeric deficit.
            verdict = {"admittable": False, "binding": "cpu", "deficit_cpu": 0, "at": now}
        by_host[host] = {"at": now, "waiting": waiting, "verdict": verdict, "consumed": consumed}
        doc.update(schema=SCHEMA, workload=workload, by_host=by_host, consumed=consumed)
        _replace(path, doc)
    state = by_host[host]
    if op == "enqueue":
        ordered = sorted(waiting, key=lambda u: (not u.get("asap"), u["at"], u["id"]))
        unit = next((u for u in ordered if u["id"] == req["id"]), None)
        state = {**state, "arrival": unit["at"] if unit else None,
                 "position": next((i + 1 for i, u in enumerate(ordered) if u["id"] == req["id"]), None)}
    return state


def waiting_window(units, registry=None):
    """Shared publisher window: urgency, observed arrival, deterministic stable ID."""
    registry = registry or admission.Priority()
    return sorted(units, key=lambda u: (u.get("asap") is not True, u.get("at", math.inf),
                                        str(u.get("id", ""))))[:registry.door_window_units]


def _demand_of(doc, now: float, max_age: float, host: str,
               resolve: Optional[Callable[[str], Optional[str]]] = None) -> Optional[admission.Demand]:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return None
    wl, at, q, unit = doc.get("workload"), doc.get("at"), doc.get("queued"), doc.get("unit")
    if not (isinstance(wl, str) and wl and _num(at) and isinstance(q, int) and not isinstance(q, bool)
            and isinstance(unit, dict)):
        return None
    if now - float(at) > max_age or float(at) - now > max_age:
        return None
    hosts = doc.get("hosts")
    if hosts is not None:
        if not isinstance(hosts, list):
            return None
        # with a resolver (placement's HostTable.resolve), an alias names its canonical host (Prism 364936434c3d)
        want = (resolve(host) if resolve else None) or host
        host_at = doc.get("host_at")
        if isinstance(host_at, dict):
            # per-host freshness: a host whose own publisher stopped expires on its own clock (t_430834ff)
            hosts = [h for h in hosts if _num(host_at.get(str(h))) and abs(now - host_at[str(h)]) <= max_age]
        names = {((resolve(str(h)) if resolve else None) or str(h)).lower() for h in hosts}
        if want.lower() not in names:
            return None
    cpus, ram, gpu = unit.get("cpus"), unit.get("ram_gb"), unit.get("gpu_mib", 0.0)
    if not (_num(cpus) and _num(ram) and _num(gpu) and cpus >= 0 and ram >= 0 and gpu >= 0):
        return None
    return admission.Demand(wl, q, admission.Estimate(float(cpus), float(ram), float(gpu), f"demand:{wl}"))


def read_demand(host: str, *, root=None, now: Optional[float] = None, max_age: Optional[float] = None,
                resolve: Optional[Callable[[str], Optional[str]]] = None,
                full_queue: bool = False) -> List[admission.Demand]:
    """Every FRESH class queue that may run on ``host``. ``resolve`` (name -> canonical host id or None) folds aliases
    on both sides of a ``hosts`` restriction; without it the match is case-insensitive string equality.
    ``full_queue`` retains all fresh units for lookup/authorization; publication uses the bounded default."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    out: List[admission.Demand] = []
    d = _root(root) / DEMAND_REL
    try:
        names = sorted(p for p in d.glob("*.json"))
    except OSError:
        return out
    for p in names:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _say(f"demand:{p}", f"{p} unreadable: holds nothing back")
            continue
        workload = doc.get("workload") if isinstance(doc, dict) else None
        if workload != p.stem or not _owned(p, workload, root):
            continue
        dm = _demand_of(doc, now, max_age, host, resolve)
        by_host = doc.get("by_host", {})
        canonical = (resolve(host) if resolve else None) or host
        state = next((s for h, s in by_host.items()
                      if (((resolve(h) if resolve else None) or h).lower() == canonical.lower())), None) if isinstance(by_host, dict) else None
        if isinstance(state, dict) and _num(state.get("at")) and abs(now - state["at"]) <= max_age:
            waiting = []
            for u in state.get("waiting", []):
                if not isinstance(u, dict):
                    continue
                if u.get("id") in consumed_units(doc):
                    continue
                seen_at = u.get("seen_at", state["at"])
                if not _num(seen_at) or not 0 <= now - seen_at <= max_age:
                    continue
                # freshness is the heartbeat (seen_at, checked above); arrival is only the ordering key, so a live
                # unit re-asserted for more than 7 days stays visible and authorizable (t_430834ff)
                at, cpu, mem = u.get("at"), u.get("need_cpu"), u.get("need_mem_gb")
                if not (_num(at) and _num(cpu) and cpu > 0 and _num(mem) and mem >= 0):
                    continue
                waiting.append(admission.WaitingUnit(min(at, now), admission.Estimate(cpu, mem),
                                                      u.get("asap") is True, str(u.get("id", ""))))
            waiting.sort(key=lambda u: (not u.asap, u.at, u.id))
            if not full_queue:
                waiting = waiting[:load_priority(root=root).door_window_units]
            v = state.get("verdict")
            verdict = None
            if (isinstance(v, dict) and isinstance(v.get("admittable"), (bool, int))
                    and isinstance(v.get("binding"), str) and _num(v.get("deficit_cpu"))
                    and v["deficit_cpu"] >= 0 and _num(v.get("at"))):
                verdict = admission.DoorVerdict(int(v["admittable"]), v["binding"], v["deficit_cpu"], min(v["at"], now))
            unit = dm.unit if dm else (waiting[0].need if waiting else admission.Estimate())
            dm = admission.Demand(workload, dm.queued if dm else len(waiting), unit,
                                  tuple(waiting), verdict, state["at"])
        if dm is not None:
            out.append(dm)
    return out


def door_document(d: admission.Demand) -> dict:
    return {"workload": d.workload, "queued": d.queued, "at": d.at,
            "unit": dataclasses.asdict(d.unit),
            "waiting": [{"id": u.id, "at": u.at, "need_cpu": u.need.cpus,
                         "need_mem_gb": u.need.ram_gb, "asap": u.asap} for u in d.waiting],
            "verdict": dataclasses.asdict(d.verdict) if d.verdict else None}


def doors_from_document(doc: Mapping, *, now: float) -> List[admission.Demand]:
    """Decode the common GET transport without consumer-side priority logic."""
    if doc.get("schema") != SCHEMA or not isinstance(doc.get("host"), str):
        raise ValueError("invalid demand response")
    registry = priority_from_doc(doc.get("priority", {}))
    out = []
    for row in doc.get("door", []):
        # Reuse the disk parser's validation/freshness rules through a synthetic host doc.
        if not isinstance(row, dict) or not _name(row.get("workload")):
            continue
        at = row.get("at")
        if not _num(at) or abs(now - at) > registry.demand_max_age_s:
            continue
        waiting = []
        for u in row.get("waiting", [])[:registry.door_window_units]:
            if not isinstance(u, dict):
                continue
            t, cpu, mem = u.get("at"), u.get("need_cpu"), u.get("need_mem_gb")
            # the row's ``at`` (server publication) is the freshness check above; arrival only orders (t_430834ff)
            if not (_num(t) and _num(cpu) and cpu > 0 and _num(mem) and mem >= 0):
                continue
            waiting.append(admission.WaitingUnit(min(t, now), admission.Estimate(cpu, mem),
                                                  u.get("asap") is True, str(u.get("id", ""))))
        v = row.get("verdict")
        verdict = None
        if (isinstance(v, dict) and type(v.get("admittable")) in (int, bool)
                and isinstance(v.get("binding"), str) and _num(v.get("deficit_cpu"))
                and v["deficit_cpu"] >= 0 and _num(v.get("at"))):
            verdict = admission.DoorVerdict(int(v["admittable"]), v["binding"], v["deficit_cpu"], min(v["at"], now))
        unit = row.get("unit", {})
        need = admission.Estimate(unit.get("cpus", 0), unit.get("ram_gb", 0), unit.get("gpu_mib", 0))
        # the aggregate queue count is its own fact (legacy holdback), never the bounded window's length (6e3523a0dbe8)
        q = row.get("queued")
        queued = q if type(q) is int and q >= 0 else len(waiting)
        out.append(admission.Demand(row["workload"], queued, need, tuple(waiting), verdict, at))
    return out


def unified(workload: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """<WORKLOAD>_UNIFIED_ADMIT (e.g. PRISM_UNIFIED_ADMIT, TEST_GATE_UNIFIED_ADMIT): "0" = the caller's old rule."""
    env = os.environ if env is None else env
    key = workload.upper().replace("-", "_") + "_UNIFIED_ADMIT"
    return str(env.get(key, "1")).strip() != "0"


def published_unified(workload: str, *, root=None, now: Optional[float] = None,
                      max_age: Optional[float] = None) -> Optional[bool]:
    """The kill switch as ``workload``'s admitter last published it (var/placement/demand/<workload>.json ``unified``),
    or None: no file, unreadable, malformed, no ``unified`` key (an older publisher) or older than demand_max_age_s.
    A reader in ANOTHER process than the admitter reads the mode here, never its own env (Prism 59574d3a240b)."""
    now = float(time.time() if now is None else now)
    max_age = max_age_s(root=root) if max_age is None else float(max_age)
    try:
        doc = json.loads((_root(root) / DEMAND_REL / f"{workload}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("workload") != workload:
        return None
    at, mode = doc.get("at"), doc.get("unified")
    if not _num(at) or abs(now - float(at)) > max_age or not isinstance(mode, bool):
        return None
    return mode


def workload_of(consumer: str) -> str:
    """placement consumer -> admission workload class (the names in fleet/admission-priority.json)."""
    return {"athena": "kanban"}.get(consumer, consumer)

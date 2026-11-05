#!/usr/bin/env python3
"""public-workflow-allowlist-lint: on the PUBLIC mirror (ANG-Repositories/hermes-agent) only the workflows
named in .github/ci/public-workflow-allowlist.txt may start runs by themselves (card t_868e850a; ruling
ruling:t_a4fdb8ba-archive-perm-loadguard-set-gbrain-home: 11 workflows, everything else dispatch-only).

WHY: GitHub disabled Actions on ANG-Ventures/hermes-agent for run volume (~1,900 runs/day). A workflow added
or re-armed with a push/pull_request/schedule/... trigger would silently put that volume back.

RULES (one line per finding; exit 1 on any, 0 clean, 2 unreadable):
  1. not-allowlisted: a workflow file outside the allowlist has a trigger other than workflow_dispatch /
     workflow_call (workflow_call is kept: a live workflow may call the file, and a call cannot self-start).
  2. missing: an allowlist line names a file that does not exist.
  3. self-hosted: a runs-on reachable from an allowlisted workflow (itself or a local `uses:` callee)
     names a literal `self-hosted` label without gating it on a `vars.` expression. Var-gated labels fall
     through to a hosted runner when the repo/org variable is unset, which is the public repo's state.

  public-workflow-allowlist-lint.py [--repo DIR] [--json]
KILL SWITCH: PUBLIC_WORKFLOW_ALLOWLIST=off -> prints `skipped` and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

ALLOWLIST = ".github/ci/public-workflow-allowlist.txt"
WORKFLOWS = ".github/workflows"
INERT = {"workflow_dispatch", "workflow_call"}
LOCAL_USES = re.compile(r"^\./\.github/workflows/([^@\s]+)$")


def load_allowlist(root: pathlib.Path) -> list[str]:
    out = []
    for ln in (root / ALLOWLIST).read_text(encoding="utf-8-sig").splitlines():
        ln = ln.split("#", 1)[0].strip()
        if ln:
            out.append(ln)
    return out


def triggers(doc) -> set[str]:
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True.
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None
    if on is None:
        return set()
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def runs_on_values(job) -> list[str]:
    ro = job.get("runs-on")
    if ro is None:
        return []
    if isinstance(ro, (list, dict)):
        return [json.dumps(ro)]
    return [str(ro)]


def lint(root: pathlib.Path) -> list[str]:
    findings: list[str] = []
    wf_dir = root / WORKFLOWS
    try:
        allow = load_allowlist(root)
    except OSError as e:
        raise SystemExit(f"public-workflow-allowlist-lint: cannot read {ALLOWLIST}: {e}") from None
    allowed = set(allow)
    docs: dict[str, dict] = {}
    for f in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        try:
            docs[f.name] = yaml.safe_load(f.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise SystemExit(f"public-workflow-allowlist-lint: cannot parse {f}: {e}") from None

    for path in allow:
        if not (root / path).is_file():
            findings.append(f"{path}: missing (allowlist line names no file)")

    for name, doc in docs.items():
        rel = f"{WORKFLOWS}/{name}"
        if rel in allowed:
            continue
        live = sorted(triggers(doc) - INERT)
        if live:
            findings.append(f"{rel}: not-allowlisted but self-triggers on {','.join(live)} "
                            f"(reduce `on:` to workflow_dispatch/workflow_call, or add it to {ALLOWLIST} with a card)")

    # Rule 3: walk local `uses:` from every allowlisted workflow.
    seen: set[str] = set()
    stack = [p.rsplit("/", 1)[-1] for p in allow]
    while stack:
        name = stack.pop()
        if name in seen or name not in docs:
            continue
        seen.add(name)
        for jid, job in ((docs[name].get("jobs") or {}).items()):
            if not isinstance(job, dict):
                continue
            m = LOCAL_USES.match(str(job.get("uses", "")))
            if m:
                stack.append(m.group(1))
            for ro in runs_on_values(job):
                if "self-hosted" in ro and "vars." not in ro:
                    findings.append(f"{WORKFLOWS}/{name}: job {jid} runs-on names self-hosted with no vars. gate: {ro[:120]}")
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if os.environ.get("PUBLIC_WORKFLOW_ALLOWLIST", "").strip().lower() == "off":
        print("public-workflow-allowlist-lint: skipped (PUBLIC_WORKFLOW_ALLOWLIST=off)")
        return 0
    try:
        findings = lint(pathlib.Path(a.repo))
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"findings": findings}))
    else:
        for f in findings:
            print(f"FINDING {f}")
        print(f"public-workflow-allowlist-lint: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

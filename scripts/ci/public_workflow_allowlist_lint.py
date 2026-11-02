#!/usr/bin/env python3
"""public-workflow-allowlist-lint: the public repo's workflow load guard (plan 2026-11-01
public-repo-actions-migration §3a step 5, card t_a4fdb8ba).

WHY: GitHub disabled Actions on ANG-Ventures/hermes-agent for run volume (1,906 runs on 10-05). The public
copy runs only the workflows .github/ci/public-workflow-allowlist.txt lists as `live:`/`callee:`; everything
else that can start a run by itself is `disabled:` and kept `disabled_manually` on GitHub.

STATIC RULES (default; exit 1 on any finding, 0 clean, 2 unreadable):
  1. Every workflow file with a trigger other than `workflow_call` has exactly one line in the allowlist.
  2. Every allowlist line names an existing workflow file; `callee:` lines carry a `# <why>`.
  3. A `disabled:` workflow is not called (`uses: ./.github/workflows/<file>`) by a live one.

LIVE MODE (`--live`, needs GH_TOKEN with actions read on `repo:`): GitHub's workflow state must agree:
  every `disabled:` file is `disabled_manually`, every other file is `active`. A workflow someone enabled
  without an allowlist line is a finding. `--apply` (needs actions write) PUTs the disable/enable calls
  to make GitHub match the file, then re-reads.

  public-workflow-allowlist-lint [--repo DIR] [--live [--apply]] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    print("public-workflow-allowlist-lint: pyyaml missing", file=sys.stderr)
    sys.exit(2)

ALLOWLIST = pathlib.Path(".github") / "ci" / "public-workflow-allowlist.txt"
KINDS = ("live", "callee", "disabled")
LINE = re.compile(r"^(live|callee|disabled):\s*(\S+)\s*(?:#\s*(.*\S))?\s*$")
CALL = re.compile(r"uses:\s*['\"]?\./\.github/workflows/([\w.-]+)")


def parse_allowlist(text: str) -> tuple[str, dict[str, tuple[str, str]], list[str]]:
    repo, entries, errs = "", {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("repo:"):
            repo = line.split(":", 1)[1].strip()
            continue
        m = LINE.match(line)
        if not m:
            errs.append("allowlist line %d: not `<live|callee|disabled>: <file>`: %s" % (n, line))
            continue
        kind, name, why = m.group(1), m.group(2), m.group(3) or ""
        if name in entries:
            errs.append("allowlist line %d: %s listed twice" % (n, name))
        if kind == "callee" and not why:
            errs.append("allowlist line %d: callee %s needs `# <why>`" % (n, name))
        entries[name] = (kind, why)
    if not repo:
        errs.append("allowlist: no `repo: <owner/name>` line")
    return repo, entries, errs


def _triggers(doc) -> set[str]:
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None  # YAML 1.1: bare `on` is True
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(k) for k in on}
    return set()


def workflows(repo: pathlib.Path) -> dict[str, str]:
    wf = repo / ".github" / "workflows"
    if not wf.is_dir():
        raise OSError("no workflows directory: %s" % wf)
    return {p.name: p.read_text(encoding="utf-8-sig") for p in sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml")))}


def static_findings(repo: pathlib.Path) -> tuple[str, dict[str, tuple[str, str]], list[str]]:
    al = repo / ALLOWLIST
    if not al.is_file():
        raise OSError("no allowlist: %s" % al)
    name, entries, out = parse_allowlist(al.read_text(encoding="utf-8"))
    files = workflows(repo)
    for f, text in files.items():
        try:
            trig = _triggers(yaml.safe_load(text))
        except yaml.YAMLError as e:
            out.append("%s: unparseable YAML: %s" % (f, e.__class__.__name__))
            continue
        if trig - {"workflow_call"} and f not in entries:
            out.append("%s: triggers %s but has no allowlist line; add `disabled: %s` (or `live:` with a "
                       "reviewed reason) to %s" % (f, sorted(trig - {"workflow_call"}), f, ALLOWLIST, ))
    for f in entries:
        if f not in files:
            out.append("allowlist names %s, which is not in .github/workflows" % f)
    disabled = {f for f, (k, _w) in entries.items() if k == "disabled"}
    for f, (k, _w) in entries.items():
        if k == "disabled" or f not in files:
            continue
        for callee in CALL.findall(files[f]):
            if callee in disabled:
                out.append("%s is `%s` and calls %s, which is `disabled`: a disabled callee breaks the live run"
                           % (f, k, callee))
    return name, entries, out


def _api(method: str, url: str, token: str):
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None


def live_state(name: str, token: str) -> dict[str, tuple[int, str]]:
    out, page = {}, 1
    while True:
        d = _api("GET", "https://api.github.com/repos/%s/actions/workflows?per_page=100&page=%d" % (name, page), token)
        for w in d.get("workflows", []):
            path = w.get("path", "")
            if path.startswith(".github/workflows/"):
                out[path.rsplit("/", 1)[1]] = (w["id"], w["state"])
        if len(d.get("workflows", [])) < 100:
            return out
        page += 1


def live_findings(entries: dict[str, tuple[str, str]], state: dict[str, tuple[int, str]]) -> list[tuple[str, str]]:
    """(file, wanted state) for every workflow whose GitHub state disagrees with the allowlist."""
    out = []
    for f, (_id, st) in sorted(state.items()):
        want = "disabled_manually" if entries.get(f, ("", ""))[0] == "disabled" else "active"
        if st != want:
            out.append((f, want))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path("."))
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        name, entries, found = static_findings(a.repo)
    except OSError as e:
        print("public-workflow-allowlist-lint: unreadable: %s" % e, file=sys.stderr)
        return 2
    drift = []
    if a.live:
        token = os.environ.get("GH_TOKEN", "")
        if not token or not name:
            print("public-workflow-allowlist-lint: --live needs GH_TOKEN and a `repo:` line", file=sys.stderr)
            return 2
        state = live_state(name, token)
        drift = live_findings(entries, state)
        if a.apply and drift and not found:
            for f, want in drift:
                verb = "disable" if want == "disabled_manually" else "enable"
                _api("PUT", "https://api.github.com/repos/%s/actions/workflows/%d/%s" % (name, state[f][0], verb), token)
                print("applied: %s %s" % (verb, f))
            drift = live_findings(entries, live_state(name, token))
        found += ["%s on %s is not %s (allowlist)" % (f, name, want) for f, want in drift]
    if a.json:
        print(json.dumps({"findings": found}, indent=1))
    else:
        for f in found:
            print("FAIL public-workflow-allowlist: " + f)
        print("public-workflow-allowlist-lint: %d finding(s)" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

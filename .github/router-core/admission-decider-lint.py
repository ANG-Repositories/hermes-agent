#!/usr/bin/env python3
# SCHEDULE: manual (CI lint, suite admission_tests; operators run it with --update after moving a decider)
"""admission-decider-lint: refuse a NEW place that decides "may this work start on host H now" outside the ONE
admission library (t_e5124426; same shape as the reserved-lane lint of t_a115fc8a).

The library is scripts/lib/admission.py (admit / admit_class), scripts/lib/admission_demand.py and
scripts/lib/placement.py (may_start / choose / admit, which call it). Any other tracked script that compares a host
load metric to a threshold (cpu_busy, load1/load5, procs_running, runnable_ewma, occupancy, PSI cpu, load_ratio,
getloadavg, ncpu // N) is a candidate private admission rule. The ones that existed when this lint landed are
grandfathered in fleet/admission-decider-baseline.json with their hit COUNT (most are pagers, watchdogs and
forensics, not deciders; the inventory plans/2026-10-27_unified-admission-INVENTORY.md classifies them). The lint is
a ratchet:
  * a file NOT in the baseline with any hit             -> FAIL (route the decision through placement.may_start /
                                                            admission.admit_class, or, for a monitor that never
                                                            gates work, add `# admission-lint: not-a-decider <why>`
                                                            on the line)
  * a baselined file whose hit count GREW               -> FAIL (same remedy)
  * a baselined file whose count shrank / that is gone  -> FAIL until the SAME PR lowers the baseline (--update
                                                            rewrites it down, never up), so a removed decider
                                                            cannot silently grow back into its old allowance
  * --base-ref SHA (CI on a PR): the baseline itself may only go DOWN against the PR base. A row added or raised
    is refused unless the base TREE already carried that many hits in that file (recording code that is already on
    main, never new code); a widened `library` or narrowed `scan` is refused; no baseline at base = a repo's first
    baseline, allowed.

One lint, four repos (t_22955b73): hermes-home owns it; fleet-ops-scripts, prism-router and hermes-agent vendor it
byte-identical under .github/router-core/ (PIN.json) with their own fleet/admission-decider-baseline.json. A baseline
may carry `scan` (dirs, "." = whole tree); `library` is required (repo paths of that repo's admission library copy).
Absent scan uses hermes-home's defaults below. scripts/admission-decider-census.py sums the four baselines into one line.

  admission-decider-lint.py [--root DIR] [--update] [--base-ref SHA]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from scan_scope import is_live_source  # noqa: E402  scripts/retired/ + scripts/attic/: not live source

# hermes-home: scripts/; a vendored copy: .github/router-core/ (two levels below the repo root either way)
ROOT = Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "scripts" else 2]
BASELINE_REL = Path("fleet") / "admission-decider-baseline.json"
SCAN = ("scripts", "fleet", "hooks", "bin", "cron")
LIBRARY = frozenset({"scripts/lib/admission.py", "scripts/lib/admission_demand.py", "scripts/lib/placement.py",
                     "scripts/admission-decider-lint.py"})
METRIC = (r"\b(?:cpu_busy|load1|load5|procs_running|runnable_ewma|occupancy|psi_cpu_some10|psi_cpu_some60|"
          r"load_ratio|loadavg|getloadavg)\b")
OP = r"(?:>=|<=|(?<![-=])>(?!>)|(?<!<)<(?![<=])|\s-(?:gt|ge|lt|le)\s)"
OPERAND = r"[^\n=;<>]{0,40}?"
RX = re.compile(METRIC + OPERAND + OP + r"|" + OP + OPERAND + METRIC +
                r"|\bncpu\s*//\s*\d|\bos\.getloadavg\(")
SKIP = re.compile(r"(^|/)(tests?|fixtures|\.venv)/|(^|/)test_[^/]*$|_test\.py$|selftest"
                  r"|\.(md|json|ya?ml|txt|golden|plist|service|timer|conf|nft|env)$")
WAIVER = "admission-lint: not-a-decider"
# the lint and its census, wherever a repo keeps them (their regex/doc text matches RX)
SELF = frozenset({"scripts/admission-decider-lint.py", "scripts/admission-decider-census.py",
                  ".github/router-core/admission-decider-lint.py"})


_GIT_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


class ScanError(Exception):
    """A tracked file exists but cannot be read: scanning and --update abort (Prism b63467246d5d)."""


def _git_env():
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV}


def tracked(root: Path, scan_dirs=SCAN, require_git=False):
    """git ls-files under SCAN when ``root`` IS the git worktree root; any other root (test-gate's synced remote tree,
    an export nested inside an unrelated checkout, Prism f121859a42a0) is walked."""
    env = _git_env()
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, env=env)
    same = top.returncode == 0 and Path(top.stdout.decode().strip()).resolve() == root.resolve()
    if same:
        out = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-files", "-z", "--", *scan_dirs], capture_output=True, env=env)
        if out.returncode == 0:
            return [p for p in out.stdout.decode().split("\0") if p]
    if same or require_git:
        raise ScanError("git-tracked scan required; walk fallback is read-only")
    return walk(root, scan_dirs)


def walk(root: Path, scan_dirs=SCAN):
    """Every regular file under SCAN. Path.rglob() silently skips a directory it cannot list, which would read an
    unreadable subtree as absent code and let --update drop its baseline entries (Prism 5cf1d79ce0fc); os.walk()
    reports each enumeration failure, and any failure aborts the scan as ScanError."""
    errs = []
    out = []
    for d in scan_dirs:
        top = root / d
        if not top.is_dir():
            continue
        for dirpath, dirs, files in os.walk(top, onerror=errs.append):
            if ".git" in dirs:
                dirs.remove(".git")
            for f in files:
                p = Path(dirpath) / f
                if p.is_file():
                    out.append(p.relative_to(root).as_posix())
    if errs:
        e = errs[0]
        where = getattr(e, "filename", None) or "?"
        raise ScanError(f"{where}: cannot list ({e}){f' (+{len(errs) - 1} more)' if len(errs) > 1 else ''}")
    return sorted(out)


def hits_in(text: str) -> int:
    # Physical-line ratchet, not a lexer: preserve shell expansions and quoted hashes.
    n = 0
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#") or s.startswith("//") or WAIVER in line:
            continue
        if RX.search(line):
            n += 1
    return n


def index_blob(root: Path, rel: str) -> bytes:
    """Missing tracked source is not a deletion until removed from the index."""
    env = _git_env()
    entry = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-files", "--stage", "-z", "--", rel],
                           capture_output=True, env=env)
    records = entry.stdout.rstrip(b"\0").split(b"\0")
    header, _, name = records[0].partition(b"\t")
    fields = header.split()
    if (entry.returncode or len(records) != 1 or len(fields) != 3 or fields[2] != b"0" or
            name != rel.encode() or fields[0] not in (b"100644", b"100755")):
        raise ScanError(f"{rel}: unreadable worktree file; no regular index blob (missing symlink target or conflict)")
    blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", fields[1].decode("ascii")],
                          capture_output=True, env=env)
    if blob.returncode:
        raise ScanError(f"{rel}: index blob not readable")
    return blob.stdout


def scan(root: Path, scan_dirs=SCAN, library=LIBRARY, require_git=False) -> dict:
    out = {}
    for rel in tracked(root, scan_dirs, require_git=require_git):
        if rel in library or rel in SELF or SKIP.search(rel) or not is_live_source(rel):
            continue
        try:
            raw = (root / rel).read_bytes()
        except FileNotFoundError:
            raw = index_blob(root, rel)
        except OSError as exc:
            raise ScanError(f"{rel}: unreadable ({exc})") from exc
        if b"\0" in raw[:2048]:
            continue
        n = hits_in(raw.decode("utf-8", errors="replace"))
        if n:
            out[rel] = n
    return out


def config(doc: dict):
    """(scan dirs, library paths) a baseline declares; hermes-home's defaults when absent."""
    dirs = doc.get("scan", SCAN)
    if not isinstance(dirs, (list, tuple)) or not dirs:
        raise ScanError("scan must be a nonempty list of literal relative directories")
    for d in dirs:
        if (not isinstance(d, str) or not d or d.startswith("/") or
                any(c in d for c in ":*?[]\\\n\r\0") or ".." in d.split("/") or
                (d != "." and any(part in ("", ".") for part in d.split("/")))):
            raise ScanError(f"scan path must be a literal relative directory: {d!r}")
    library = doc.get("library")
    if not isinstance(library, list) or not all(isinstance(p, str) for p in library):
        raise ScanError("library is required and must be a list of repo-relative paths")
    return tuple(dirs), frozenset(library)


def _show(root: Path, ref: str, rel: str):
    """File text at ref; only a confirmed absent tree entry returns None. Unreadable objects abort."""
    env = _git_env()
    ok = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, env=env)
    if ok.returncode != 0:
        raise ScanError(f"--base-ref {ref}: commit not readable (fetch it first)")
    tree = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-tree", "-z", ref, "--", rel],
                          capture_output=True, env=env)
    if tree.returncode != 0:
        raise ScanError(f"--base-ref {ref}: {rel}: tree not readable (fetch it first)")
    if not tree.stdout:
        return None
    entries = tree.stdout.rstrip(b"\0").split(b"\0")
    header, _, name = entries[0].partition(b"\t")
    fields = header.split()
    if len(entries) != 1 or len(fields) != 3 or fields[1] != b"blob" or name != rel.encode("utf-8"):
        raise ScanError(f"--base-ref {ref}: {rel}: expected one blob entry")
    p = subprocess.run(["git", "-C", str(root), "cat-file", "blob", fields[2].decode("ascii")],
                       capture_output=True, env=env)
    if p.returncode != 0:
        raise ScanError(f"--base-ref {ref}: {rel}: blob not readable (fetch it first)")
    return p.stdout.decode("utf-8", errors="replace")


def baseline_drift(root: Path, ref: str, head_doc: dict) -> list:
    """The baseline may only go DOWN against the PR base (t_22955b73). A row added or raised is allowed only up to the
    hits that file ALREADY had in the base tree (recording code that is on main, never code this PR adds)."""
    raw = _show(root, ref, BASELINE_REL.as_posix())
    if raw is None:
        print(f"admission-decider-lint: no baseline at base {ref[:12]}: first baseline, nothing to compare")
        return []
    try:
        base_doc = json.loads(raw)
    except ValueError as exc:
        raise ScanError(f"baseline at base {ref[:12]} unparseable ({exc})") from exc
    bad = []
    # Pre-migration home baselines omitted library; preserve the old fixed scope only
    # for historical comparison. Current baselines must always declare it.
    if "library" not in base_doc:
        base_doc["library"] = sorted(LIBRARY)
    (b_scan, b_lib), (h_scan, h_lib) = config(base_doc), config(head_doc)
    if h_lib - b_lib:
        bad.append(f"library widened against base: {sorted(h_lib - b_lib)}")
    if "." not in h_scan and set(b_scan) - set(h_scan):
        bad.append(f"scan narrowed against base: {sorted(set(b_scan) - set(h_scan))}")
    b_files = base_doc.get("files", {})
    for rel, n in sorted(head_doc.get("files", {}).items()):
        was = b_files.get(rel)
        if was is not None and n <= was:
            continue
        text = _show(root, ref, rel)
        on_base = hits_in(text) if text is not None else 0
        if n <= on_base:
            continue                  # code already on main, now recorded
        bad.append(f"{rel}: baseline row added ({n})" if was is None else f"{rel}: baseline raised {was} -> {n}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--update", action="store_true", help="rewrite the baseline DOWN to the current counts")
    ap.add_argument("--base-ref", default="", help="PR base commit: refuse a baseline that grew against it")
    a = ap.parse_args(argv)
    bp = a.root / BASELINE_REL
    try:
        doc = json.loads(bp.read_text(encoding="utf-8"))
        base = doc.get("files", {})
    except (OSError, ValueError) as exc:
        print(f"admission-decider-lint: FAIL baseline {bp} unreadable ({exc})")
        return 2
    try:
        scan_dirs, library = config(doc)
        if a.base_ref and set(a.base_ref) == {"0"}:
            raise ScanError(f"--base-ref {a.base_ref}: null sha")
        now = scan(a.root, scan_dirs, library, require_git=a.update)
        grew = baseline_drift(a.root, a.base_ref, doc) if a.base_ref else []
    except ScanError as exc:
        print(f"admission-decider-lint: FAIL scan aborted, baseline untouched: {exc}")
        return 2
    bad = []
    for rel, n in sorted(now.items()):
        was = base.get(rel)
        if was is None:
            bad.append(f"{rel}: {n} new host-load comparison(s), not in the baseline")
        elif n > was:
            bad.append(f"{rel}: {n} host-load comparisons, baseline {was}")
    stale = [f"{rel}: {now.get(rel, 0)} host-load comparisons, baseline {n} (lower the baseline in this PR: --update)"
             for rel, n in sorted(base.items()) if now.get(rel, 0) < n]
    if grew:
        print("admission-decider-lint: FAIL, the baseline may only go DOWN (t_22955b73):")
        for b in grew:
            print(f"  {b}")
        return 1
    if a.update:
        if bad:
            print("admission-decider-lint: --update refuses to raise the baseline:")
            for b in bad:
                print(f"  {b}")
            return 1
        doc = json.loads(bp.read_text(encoding="utf-8"))
        doc["files"] = dict(sorted(now.items()))
        bp.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        print(f"admission-decider-lint: baseline rewritten ({len(now)} files, {sum(now.values())} hits)")
        return 0
    if bad or stale:
        if bad:
            print("admission-decider-lint: FAIL, a host-admission decision outside the ONE library (t_e5124426):")
            for b in bad:
                print(f"  {b}")
            print("  remedy: ask placement.may_start(host, consumer) / admission.admit_class(); a monitor that never "
                  f"gates work marks the line `# {WAIVER} <why>`")
        if stale:
            print("admission-decider-lint: FAIL, the baseline is above the code; lower it in the same PR (t_22955b73):")
            for b in stale:
                print(f"  {b}")
        return 1
    print(f"admission-decider-lint: ok ({len(now)} grandfathered files, {sum(now.values())} hits == baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

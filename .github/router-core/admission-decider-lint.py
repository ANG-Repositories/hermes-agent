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

Standalone setup (CI uses the same pinned manifest):
  python3 -m venv .venv-admission
  .venv-admission/bin/python -m pip install -r scripts/admission-decider-requirements.txt
  .venv-admission/bin/python scripts/admission-decider-lint.py
Vendored callers use .github/router-core/ for both paths. JS/TS/TSX require the
MIT-licensed tree-sitter dependencies; missing parsers and invalid syntax abort.
Go retains the conservative historical line scanner, not the JavaScript grammar.
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
# Prism 56a668146742: a shift inside the operand (`(procs_running >> 1) > 8`) is not a comparison; only a lone
# `<` / `>` ends the operand, a `>>` / `<<` pair is part of it.
OPERAND = r"(?:[^\n=;<>]|>>|<<){0,40}?"
RX = re.compile(METRIC + OPERAND + OP + r"|" + OP + OPERAND + METRIC +
                r"|\bncpu\s*//\s*\d|\bos\.getloadavg\(")
SKIP = re.compile(r"(^|/)(tests?|fixtures|\.venv)/|(^|/)test_[^/]*$|_test\.py$|selftest"
                  r"|\.(?:test|spec)\.[cm]?[jt]sx?$|\.(md|json|ya?ml|txt|golden|plist|service|timer|conf|nft|env)$")
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


def hits_in(text: str, lang: str = "py") -> int:
    """Count comparison lines, joining continued expressions first (Prism 6acce03c3edb: black/ruff wrap long
    conditions across lines, and shell `test ... \\` continues; a wrapped decider must still count). Not a lexer:
    no `#` stripping (a shell `"$x" -lt 90 # note` or a quoted hash is kept whole), paren/bracket depth and a
    trailing backslash decide where a logical line ends; each logical line counts at most once."""
    # Prism 9be94a192b07: delimiters inside string literals or trailing comments must not drive the join (an
    # unmatched `(` in prose joined the rest of the file into one logical line, counted once). Depth is read on
    # the line with quotes and a trailing `#`/`//` comment blanked, and a join is bounded to JOIN_MAX physical
    # lines: a wrapped condition is a handful of lines, so anything longer flushes and the lines count singly.
    # Prism 053e4801af29 / e02aba55d4a0: this is deliberately NOT a lexer, so a join can be opened by something
    # it cannot see (a `(` in a triple-quoted string body, a JS regex literal). Two defences: (a) Python
    # triple-quote state is carried ACROSS lines so string bodies never count delimiters; (b) a join that never
    # closes is UNWOUND: the buffered physical lines are counted singly (the pre-join behaviour), never once.
    # Undercounting is the failure the ratchet exists to prevent, so the fallback errs toward counting.
    if lang in ("c", "js", "ts", "tsx"):
        return _js_hits(text, lang)
    n = 0
    buffered: list[str] = []
    bares: list[str] = []
    depth = 0
    in_triple = None            # the open triple-quote delimiter while inside a Python multi-line string
    for line in text.splitlines():
        s = line.lstrip()
        if in_triple is None and (s.startswith("#") or s.startswith("//")):
            continue
        if WAIVER in line and in_triple is None:
            # Prism 83b42c6be461: the waiver covers ITS statement only. Statements already completed inside the
            # open join (an IIFE body before a waived telemetry line) still count; the open one is the waived one.
            n += _count_completed(buffered, bares)
            buffered, bares, depth = [], [], 0
            continue
        code = line.rstrip()
        bare, in_triple = _blank_line(code, lang, in_triple)
        buffered.append(code.removesuffix("\\"))
        bares.append(bare)
        depth += bare.count("(") + bare.count("[") - bare.count(")") - bare.count("]")
        if depth <= 0 and not bare.endswith("\\"):
            n += _count_joined(buffered, bares)
            buffered, bares, depth = [], [], 0
        # Do not chunk a pending expression at an arbitrary physical-line boundary: its metric and
        # operator may straddle that boundary. Input is already resident in memory; retain context
        # until the join closes (or EOF unwinds it), splitting independent statements below.
    if buffered:
        n += _count_unwound(buffered, bares) if depth > 0 else _count_joined(buffered, bares)
    return n


def _js_hits(text: str, lang: str) -> int:
    """Parse JS/TS statements; never guess ASI, template, or shift-token boundaries.

    One hit per statement (control conditions own their hit, not their bodies).
    Comments and literal text are not executable metric operands. Go deliberately
    retains the pre-parser conservative scanner via its separate language tag.
    """
    try:
        from tree_sitter import Language, Parser
        if lang in ("ts", "tsx"):
            import tree_sitter_typescript as grammar
            capsule = grammar.language_tsx() if lang == "tsx" else grammar.language_typescript()
        else:
            import tree_sitter_javascript as grammar
            capsule = grammar.language()
        parser = Parser(Language(capsule))
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise ScanError("unsupported JS/TS parser; install admission-decider-requirements.txt "
                        f"in an isolated environment ({exc})") from exc
    source = text.encode("utf-8")
    root = parser.parse(source).root_node
    if root.has_error:
        raise ScanError(f"unreadable {lang} syntax: tree-sitter rejected source")
    owners = set()
    statements = {"expression_statement", "lexical_declaration", "variable_declaration",
                  "return_statement", "throw_statement", "if_statement", "while_statement",
                  "do_statement", "for_statement", "switch_statement", "field_definition",
                  "public_field_definition", "assignment_pattern", "export_statement"}
    literals = {"string", "string_fragment", "comment", "regex"}

    def metric(node):
        stack = [node]
        while stack:
            item = stack.pop()
            if item.type == "string" and item.parent is not None and item.parent.type == "subscript_expression":
                if item.parent.child_by_field_name("index") == item and re.fullmatch(METRIC, item.text.decode()[1:-1]):
                    return True
            if item.type in literals:
                continue
            if item.type in ("identifier", "property_identifier") and re.fullmatch(METRIC, item.text.decode()):
                return True
            stack.extend(item.named_children)
        return False

    waived = set()
    stack = [(root, root)]
    while stack:
        node, owner = stack.pop()
        if node.type == "comment" and WAIVER.encode() in node.text:
            previous = node.prev_named_sibling
            if previous is not None and previous.end_point.row == node.start_point.row:
                owner = previous if previous.type in statements else owner
            waived.add((owner.start_byte, owner.end_byte))
            continue
        if node.type in literals:
            continue
        if node.type in statements:
            owner = node
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            if op is not None and op.text in (b">", b"<", b">=", b"<=") and metric(node):
                # Expressions not enclosed by a known statement own themselves;
                # they must never collapse into a whole-file/root allowance.
                target = node if owner == root else owner
                owners.add((target.start_byte, target.end_byte))
        stack.extend((child, owner) for child in node.named_children)
    return len(owners - waived)


def _count_singly(lines) -> int:
    return sum(bool(RX.search(ln)) for ln in lines)


_STMT_START = re.compile(r"^\s*(?:if|elif|else\s+if|while|for|return|assert|until|const|let|var|test)\b|^\s*(?:\[\[?\s|\(\()")


def _statements(lines, bares=None):
    """[(statement lines, closed)] for one buffered join. Prism bf35b2bc1492: an enclosing paren (a JS IIFE, a call
    wrapping a block) can hold several independent decisions, so the join is split into statements: a physical line
    whose bare text ends in `;`, `{` or `}` closes one, and a line opening `if`/`elif`/`while`/`for`/`return`/`assert`
    starts one (closing the one before it). The last statement is open unless its line closed it."""
    bares = bares if bares is not None else lines
    out, seg, seg_bares = [], [], []
    for ln, bare in zip(lines, bares):
        # ASI-style assignments may start a new statement inside an enclosing
        # callback. Split only after a complete preceding segment; wrapped RHS
        # expressions still stay together.
        member = r"[A-Za-z_$][\w$]*(?:(?:\.[A-Za-z_$][\w$]*)|(?:\[[^\]]*\]))*"
        assignment = re.match(r"\s*(?:(?:const|let|var)\s+)?" + member + r"\s*=(?!=|>)", bare)
        call = re.match(r"\s*(?:await\s+)?" + member + r"\s*\(", bare)
        shell_test = re.match(r"\s*command\s+test\b", bare)
        if seg and (_STMT_START.match(bare) or shell_test or ((assignment or call) and not _continues(seg_bares))):
            out.append((seg, True))
            seg, seg_bares = [], []
        seg.append(ln)
        seg_bares.append(bare)
        if bare.rstrip().endswith((";", "{", "}")):
            out.append((seg, True))
            seg, seg_bares = [], []
    if seg:
        out.append((seg, False))
    return out


def _count_joined(lines, bares=None) -> int:
    """One closed logical line, counted once PER STATEMENT inside it (_statements). Each statement still joins its
    own wrapped lines, so a compound condition wrapped over lines (`if (load1 > x and` / `load5 > 3):`) is one
    decision, counted once."""
    return sum(bool(RX.search(" ".join(seg))) for seg, _closed in _statements(lines, bares))


_CONTINUES = re.compile(r"(?:\\|,|&&|\|\||[-+*/%=<>?!(\[{]|\b(?:and|or|not|in|is))\s*$")


def _continues(bares) -> bool:
    """True when a buffered statement visibly runs on into the next physical line: an unclosed bracket inside the
    statement, or its last line ending in a continuation token (backslash, comma, boolean/arithmetic operator)."""
    depth = sum(b.count("(") + b.count("[") + b.count("{") - b.count(")") - b.count("]") - b.count("}")
                for b in bares)
    return depth > 0 or bool(_CONTINUES.search(bares[-1].rstrip())) if bares else False


def _count_completed(lines, bares=None) -> int:
    """The statements of an open join that a waived line does NOT cover (Prism 83b42c6be461, 81a0d5670704,
    491a76480688). The waiver covers only the statement it sits on: the trailing open statement is that one only
    when it visibly continues onto the waived line (_continues). A trailing statement that already reads complete
    (semicolon-free JS by ASI, a shell `if ...; fi`, any language's terminator) counts, so the choke point never
    depends on one language's statement terminator. Undercount is the failure the ratchet exists to prevent."""
    bares = bares if bares is not None else lines
    segs = _statements(lines, bares)
    n = 0
    k = 0
    for seg, closed in segs:
        seg_bares = bares[k:k + len(seg)]
        k += len(seg)
        if closed or not _continues(seg_bares):
            n += bool(RX.search(" ".join(seg)))
    return n


def _count_unwound(lines, bares=None) -> int:
    """A join that never closed (or hit JOIN_MAX). Prism 6b398a445eca: per statement, the larger of its physical-line
    hits and its joined hit, so a comparison split across lines (`load1` / `> 4`) still counts and the unwind never
    counts below the physical lines (the pre-join behaviour, Prism 053e4801af29)."""
    return sum(max(_count_singly(seg), int(bool(RX.search(" ".join(seg))))) for seg, _closed in _statements(lines, bares))


def _blank_line(code: str, lang: str, in_triple):
    """(bare, in_triple') for one physical line: blanks quoted spans and the trailing comment for delimiter
    counting, carrying Python triple-quote state across lines (053e4801af29).

    Prism a21209b97e0e: one left-to-right scan of the whole line, so a triple quote opens wherever it sits
    (after indentation, after `x = `, after a closed single-line string), never only at column zero."""
    if lang != "py":
        return _blank_quotes_and_comment(code, lang), None
    out = []
    q = None                     # open single-line quote char
    i, n = 0, len(code)
    while i < n:
        if in_triple:
            if code[i] == "\\":
                i += 2
                continue
            if code.startswith(in_triple, i):
                i, in_triple = i + 3, None
                continue
            i += 1
            continue
        c = code[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if code.startswith(("'''", '"""'), i):
            in_triple = code[i:i + 3]
            i += 3
            continue
        if c in ("'", '"'):
            q = c
            i += 1
            continue
        if c == "#":
            break
        out.append(c)
        i += 1
    return "".join(out), in_triple


JOIN_MAX = 8   # physical lines one logical comparison may span (black wraps a condition in 2-4)


def _blank_quotes_and_comment(code: str, lang: str = "py") -> str:
    """The line with every quoted span blanked and a trailing comment removed, for delimiter counting only (the
    comparison regex still reads the real text). Quotes are matched per line: a quote that does not close on its
    line blanks to EOL, which keeps a multi-line string from opening a join either.

    Prism fba51c46ef6f / 7cfa74e7831e: the comment marker is per language. Python: `#` only (`//` is floor
    division). Shell: `#` only when it starts a word (preceded by start/space/`;`/`(`), never inside `$#`,
    `${#arr[@]}` or `foo#bar`. JS/TS/Go-style: `//`. A wrong marker ate the closing delimiter and opened a join."""
    out = []
    q = None
    i = 0
    n = len(code)
    while i < n:
        c = code[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in ("'", '"'):
            q = c
            i += 1
            continue
        if lang == "py" and c == "#":
            break
        if lang == "sh" and c == "#" and (i == 0 or code[i - 1] in " \t;(") :
            break
        if lang in ("c", "go") and code.startswith("//", i):
            break
        out.append(c)
        i += 1
    return "".join(out)


_LANG_BY_SUFFIX = {".py": "py", ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".js": "js", ".ts": "ts", ".go": "go",
                   ".mjs": "js", ".cjs": "js", ".jsx": "js", ".tsx": "tsx"}


def _lang_of_text(rel, raw: bytes) -> str:
    """py / sh / c from the suffix, else from the shebang, else py (the conservative marker set: `#` only)."""
    import os
    suf = os.path.splitext(str(rel))[1].lower()
    if suf in _LANG_BY_SUFFIX:
        return _LANG_BY_SUFFIX[suf]
    first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    if first.startswith("#!") and ("sh" in first.rsplit("/", 1)[-1] or "bash" in first):
        return "sh"
    return "py"
    if first.startswith("#!") and ("sh" in first.split("/")[-1] or "bash" in first):
        return "sh"
    return "py"


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
        try:
            n = hits_in(raw.decode("utf-8", errors="replace"), _lang_of_text(rel, raw))
        except ScanError as exc:
            raise ScanError(f"{rel}: {exc}") from exc
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
        on_base = hits_in(text, _lang_of_text(rel, text.encode("utf-8", "replace"))) if text is not None else 0
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

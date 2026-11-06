"""lint_scope -- on a pull_request run a repo-wide lint FAILS only for findings in the PR's own files.

WHY (card t_109bdf21, Ace 2026-10-30 19:46: "decouple main being red from everything else"): a lint that walks the
whole tree failed EVERY open PR as soon as one file anywhere on main was bad. hermes-home#7911 (2 files) needed 4
lander re-arms on 10-30 for spawn-site / lint_monitor_contract / main-guard-order / always-review-derive reds, and
none of them named a file the PR touched. The one coupling kept on purpose: a finding in a file the PR adds or
modifies still fails the PR.

THE rule, in one place:
  push / local / any non-PR event  mode "full": every finding fails (main's own signal, routed by the existing pager).
  pull_request                     mode "pr": changed = `git diff --raw -z --no-renames <base> HEAD`, base = the
                                   merge commit's first parent (the main the PR was built on), falling back to
                                   $BASE_SHA, then the event's base.sha. A finding is the PR's when its owner path is
                                   in `changed`. Any other finding is MEASURED against base, and a finding that is
                                   also on base prints as
                                     INFO pre-existing on base, owner: <path>: <finding>
                                   and does not fail. Anything NOT on base is NEW on the PR and fails.
  RULE B (base is judged by BASE's lint code; Apollo r6 ruling on #7997): the base measurement is a SUBPROCESS,
    `python3 -E <base worktree>/<lint path> <args>` with cwd = the base worktree, LINT_SCOPE=full and no PYTHONPATH, so
    the lint file, its helpers and its policy files are the base revision's. Its stdout is parsed back into findings
    by the lint's own base_parse. A rule change in the PR (in the lint, a helper it imports or loads by any means, a
    policy file) shows up as a head-only finding (NEW, fails) with no dependency discovery. The base worktree cannot
    be made, the lint is absent at base, the base run crashes (rc not 0/1, a traceback, rc 1 with nothing parsed), or
    a tracked symlink in the base tree resolves into the head checkout: the run is graded full.
  RULE P (a finding is suppressible only through an exact, plain path): its owner must be a TRACKED file whose git
    path matches [A-Za-z0-9._/@-]+, has no `..` component and no leading `/`. RULE P2 (Apollo r7 ruling): a lint
    that reports text declares ONE owner delimiter (split(..., owner_delim=...)), and the owner is the text up to
    the FIRST occurrence of it, with no candidate search and no shorter-prefix fallback; a lint that cannot name one
    passes structured owners (path_of). Owner untracked or not plain, the text starting with a PR-changed path +
    delimiter, or no delimiter declared: the finding is OWNED by the PR (fails).
      lint-spawn-site.py ":"   import-closure-lint.py path_of (two shapes: `<f> imports`, `<f> sources`)
      lint_monitor_contract.py, lint_live_sqlite_reads.py, main_guard_order_lint.py, fixture-clock-lint.py: path_of
  Falls back to "full" (fail closed, reason printed) when:
    - LINT_SCOPE=full (the kill switch; set it on the PR job to restore the old behaviour),
    - the base cannot be resolved or read (an unprovable diff never narrows a lint),
    - the PR changes one of the lint's listed inputs (the lint file itself, its allowlist/baseline, this helper),
    - full_if_deleted=True and the PR deletes a file (a lint whose finding lands on the importer of a deleted path),
    - RULE S (symlinks fail closed; Apollo r1 ruling on #7997): the PR changes any symlink, or a path the lint reads
      or attributes (a listed input, a registry/exemption/policy file, a finding's path), at any component, goes
      through a symlink that is absolute, leaves the tree, dangles, loops, or lands on a PR-changed file,
    - a RULE B base run fails (above).
  LINT_SCOPE=pr + LINT_SCOPE_BASE=<sha> forces PR mode outside Actions (local check, tests).

Usage in a lint:
    import lint_scope
    scope = lint_scope.from_env(root, "spawn-site-lint", inputs=["scripts/lint-spawn-site.py"])   # inputs[0] = lint
    fail = scope.split(findings, owner_delim=":", base_args=lambda br: ["--root", br], base_parse=str.splitlines)
    fail = scope.split(rows, path_of=lambda r: r["file"], text=..., base_args=..., base_parse=...)   # structured
    return 1 if fail else 0

scripts/lints/lint-scope-lint.py fails a NEW tracked tree-scanning lint that neither imports this module nor carries
a reasoned row in scripts/lints/lint-scope-baseline.txt. Python 3.9-compatible (lint_live_sqlite_reads.py runs on
3.9..3.14).
"""
from __future__ import annotations

import atexit
import json
import re
import os
import shutil
import subprocess
import sys
from collections import Counter

SELF_REL = "scripts/lib/lint_scope.py"
_GIT_SCRUB = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")


def _env():
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


def _git(root, *args, timeout=300, raw=False):
    """stdout of a git command; raw=True returns BYTES (for -z output: no universal-newline translation, so a name
    carrying '\\r' survives; Prism 4b9d63222dfb)."""
    p = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, env=_env(), timeout=timeout)
    if p.returncode:
        err = p.stderr.decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError("git %s: rc %d: %s" % (args[0], p.returncode, err))
    return p.stdout if raw else p.stdout.decode("utf-8", "surrogateescape")


def norm(path) -> str:
    """A lint-supplied input path, repo-relative. Never rewrites a backslash (a filename character on Linux/macOS)."""
    rel = str(path)
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def lead_path(finding) -> str:
    """The path a finding line starts with: `p:12: msg`, `p: msg`, `p msg` -> p (leading spaces ignored)."""
    s = str(finding).strip()
    for i, ch in enumerate(s):
        if ch in ": \t":
            return s[:i]
    return s


def inside(root, path) -> bool:
    """True when `path`, every symlink resolved, stays inside `root` (also resolved)."""
    r, p = os.path.realpath(str(root)), os.path.realpath(str(path))
    return p == r or p.startswith(r + os.sep)


def pr_base(root) -> str:
    """The base a pull_request run is graded against: the merge commit's first parent when HEAD is this PR's merge
    commit, else $BASE_SHA, else the event's pull_request.base.sha. '' when none is known."""
    event = {}
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH") or "") as f:
            event = json.load(f).get("pull_request") or {}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        obj = _git(root, "cat-file", "-p", "HEAD")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        obj = ""
    parents = [ln.split()[1] for ln in obj.splitlines() if ln.startswith("parent ")]
    head_sha = (event.get("head") or {}).get("sha") or ""
    if len(parents) == 2 and head_sha and parents[1] == head_sha:
        return parents[0]
    return os.environ.get("BASE_SHA", "") or (event.get("base") or {}).get("sha") or ""


def ensure_commit(root, sha):
    """Make `sha` readable in a shallow CI clone. A plain depth-1 fetch first; on failure one `--refetch` (no
    negotiation, so no thin pack: the 10-30 'unresolved deltas left after unpacking' shape). Raises when unreadable."""
    def have():
        return subprocess.run(["git", "-C", str(root), "cat-file", "-e", sha + "^{commit}"], capture_output=True,
                              env=_env()).returncode == 0
    if have():
        return
    errs = []
    for extra in ([], ["--refetch"]):
        try:
            _git(root, "fetch", "-q", "--no-tags", "--depth=1", *extra, "origin", sha, timeout=600)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            errs.append(str(e)[:200])
        if have():
            return
    raise RuntimeError("base %s not readable: %s" % (sha[:12], " | ".join(errs) or "fetch left no commit"))


def _names(out) -> list:
    """`git ... -z` output -> repo paths. -z: no core.quotePath quoting of non-ASCII names. Bytes are split on NUL
    and decoded with surrogateescape, never read as text (Prism 4b9d63222dfb)."""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "surrogateescape")
    return [norm(p) for p in out.split("\0") if p]


def _raw_diff(root, base) -> list:
    """[(status, old_mode, new_mode, path)] of `git diff --raw -z --no-renames <base> HEAD`, read as bytes."""
    toks = _git(root, "diff", "--raw", "-z", "--no-renames", base, "HEAD", raw=True).split(b"\0")
    out, i = [], 0
    while i + 1 < len(toks):
        meta = toks[i].decode("ascii", "replace")
        if not meta.startswith(":"):
            i += 1
            continue
        f = meta[1:].split()
        out.append((f[4][:1], f[0], f[1], norm(toks[i + 1].decode("utf-8", "surrogateescape"))))
        i += 2
    return out


_MAX_HOPS = 40
RULE_S = True   # the symlink fail-closed rule; tests flip it in a fixture copy as the mutant
RULE_B = True   # base judged by BASE's lint code (subprocess in the base worktree); tests flip it as the mutant
RULE_P = True   # owner = one exact, plain, tracked path; tests flip it as the mutant
PLAIN = re.compile(r"^[A-Za-z0-9._/@-]+$")


def symlink_hazard(root, rel, changed=()) -> str:
    """RULE S (Apollo r1 ruling on #7997): '' when `rel` (repo-relative) is safe for PR scope, else why not.
    Safe = every component of the path resolves without a symlink, or through symlinks that are themselves NOT
    changed by the PR, whose link text is relative, and that resolve inside the worktree (so base resolves them the
    same way, inside ITS worktree), and the final target is not a PR-changed file. Anything else (a PR-changed link,
    an absolute link, a link leaving the tree, a dangling or looping link, a path that cannot be read) is a hazard:
    the caller grades the whole tree. A path that simply does not exist (deleted, base-only) is not a hazard."""
    root = os.path.realpath(str(root))
    changed = frozenset(norm(c) for c in changed)
    rel = norm(rel)
    if not rel:
        return ""
    if os.path.isabs(str(rel)) or rel == ".." or rel.startswith("../"):
        return "%s is outside the repo" % rel
    todo, cur, hops, via = [p for p in rel.split("/") if p not in ("", ".")], [], 0, []
    while todo:
        part = todo.pop(0)
        if part == "..":
            if not cur:
                return "%s resolves outside the repo (via %s)" % (rel, " -> ".join(via) or rel)
            cur.pop()
            continue
        here = "/".join(cur + [part])
        path = os.path.join(root, here)
        try:
            is_link = os.path.islink(path)
            if not is_link:
                if not os.path.lexists(path):
                    if via:
                        return "%s is a dangling symlink (%s)" % (rel, via[0])
                    return ""   # missing at head (deleted, base-only): nothing to resolve, nothing to borrow
                cur.append(part)
                continue
            target = os.readlink(path)
        except (OSError, ValueError) as e:
            return "%s cannot be resolved (%s)" % (rel, e)
        hops += 1
        via.append(here)
        if hops > _MAX_HOPS:
            return "%s cannot be resolved (symlink loop)" % rel
        if here in changed:
            return "%s goes through symlink %s, which this PR changes" % (rel, here)
        if os.path.isabs(target):
            # resolved in the base worktree it would still land on THIS checkout (or outside the repo): base
            # would read head's bytes (Prism 9c7e9c58b773 / a573b8bf9a89)
            return "%s goes through absolute symlink %s -> %s (resolves outside the base worktree)" % (rel, here, target)
        if "\\" in target:   # a literal filename character on Linux/macOS, never a separator: do not guess
            return "%s goes through symlink %s whose target carries a backslash" % (rel, here)
        todo = [p for p in target.split("/") if p not in ("", ".")] + todo
    final = "/".join(cur)
    if via and final in changed:
        return "%s resolves through symlink %s to %s, which this PR changes" % (rel, via[0], final)
    return ""


def _drop_worktree(root, path):
    """Remove ONLY the worktree this run created; never a repo-wide `git worktree prune`, which would touch other
    sessions' worktrees (Prism e0b9c6bcd7b0)."""
    subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", path], capture_output=True, env=_env())
    shutil.rmtree(path, ignore_errors=True)


def plain(path) -> bool:
    """RULE P: a git path that can own a pre-existing finding: [A-Za-z0-9._/@-]+, no `..` component, no leading `/`."""
    p = str(path)
    return bool(PLAIN.fullmatch(p)) and not p.startswith("/") and ".." not in p.split("/")   # d9475aa988: whole path


class BaseRunError(RuntimeError):
    pass


class Scope:
    _seq = 0

    def __init__(self, lint, mode, base="", changed=(), reason="", root=None, lint_path=""):
        self.lint, self.mode, self.base, self.reason, self.root = lint, mode, base, reason, root
        self.lint_path = lint_path
        self.changed = frozenset(norm(p) for p in changed)
        self.info_count = 0
        self._base_root = None
        self._tracked = None

    def base_root(self) -> str:
        """A detached worktree of the base commit (made once per process, removed at exit). It sits next to the git
        dir, on the checkout's own disk (never a RAM /tmp). Raises when it cannot be made."""
        if self._base_root is None:
            common = _git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
            Scope._seq += 1   # two Scopes on one repo+base in one process get their own tree (Prism bc671e3582ff)
            path = os.path.join(common, "lint-scope-base-%d-%d-%s" % (os.getpid(), Scope._seq, self.base[:12]))
            shutil.rmtree(path, ignore_errors=True)
            _git(self.root, "worktree", "add", "-q", "--detach", path, self.base, timeout=900)
            atexit.register(_drop_worktree, self.root, path)
            self._base_root = path
        return self._base_root

    # ------------------------------------------------------------------------------------------------ RULE B
    def _borrows_head(self, br) -> str:
        """'' unless a tracked symlink in the base tree resolves into the HEAD checkout (outside the base tree): the
        base run would read head's bytes through it (the a573b8bf9a89 / 9c7e9c58b773 class)."""
        head = os.path.realpath(str(self.root))
        out = _git(br, "ls-files", "-s", "-z", raw=True)
        for ent in out.split(b"\0"):
            if not ent.startswith(b"120000 "):
                continue
            rel = ent.split(b"\t", 1)[1].decode("utf-8", "surrogateescape")
            real = os.path.realpath(os.path.join(br, rel))
            if inside(head, real) and not inside(br, real):
                return "base symlink %s resolves into the head checkout (%s)" % (rel, real)
            # Prism 0eb039f2d5: a tracked DIRECTORY link whose target lies outside the base tree can hold a policy or
            # helper that resolves into head beneath it; the base run cannot be proven to read base bytes through it.
            if os.path.isdir(real) and not inside(br, real):
                return "base directory symlink %s escapes the base tree (%s); descendants cannot be proven base" % (rel, real)
        return ""

    def base_run(self, base_args, base_parse) -> list:
        """RULE B: run BASE's lint (`python3 -E <base tree>/<lint path> <args>`, cwd the base tree, LINT_SCOPE=full,
        no GITHUB_*/PYTHON* env) and parse its stdout with the lint's own base_parse. Raises BaseRunError when the base
        cannot be measured: no lint path, lint absent at base, a base link into head, a crash (rc not 0/1, a
        traceback, rc 1 with nothing parsed)."""
        if not self.lint_path:
            raise BaseRunError("no lint path (inputs[0]) to run at base")
        br = self.base_root()
        borrow = self._borrows_head(br) if RULE_S else ""   # a symlink check: part of RULE S
        if borrow:
            raise BaseRunError("rule S: " + borrow)
        code_root = br if RULE_B else os.path.realpath(str(self.root))   # mutant: head's code on base's tree
        script = os.path.join(code_root, self.lint_path)
        if not os.path.isfile(script):
            raise BaseRunError("%s is absent at base" % self.lint_path)
        env = {k: v for k, v in _env().items() if not k.startswith(("GITHUB_", "PYTHON", "LINT_SCOPE"))}
        env["LINT_SCOPE"] = "full"
        p = subprocess.run([sys.executable, "-E", script] + [str(x) for x in base_args(br)], cwd=br, env=env,
                           capture_output=True, timeout=1800)
        out = p.stdout.decode("utf-8", "surrogateescape")
        err = p.stderr.decode("utf-8", "surrogateescape")
        if p.returncode not in (0, 1) or "Traceback (most recent call last)" in err:
            raise BaseRunError("base lint rc %d: %s" % (p.returncode, (err.strip() or out.strip())[-300:]))
        found = list(base_parse(out))
        if p.returncode == 1 and not found:
            raise BaseRunError("base lint rc 1 but no finding parsed: %s" % (out.strip() or err.strip())[-300:])
        return found

    def _base_keys(self, base_args, base_parse, key):
        """Counter of base finding fingerprints, or None when base cannot be measured (the run is then FULL)."""
        if base_args is None or base_parse is None:
            print("lint-scope: %s: no base run wired; findings on unchanged files fail (unmeasured)" % self.lint,
                  file=sys.stderr)
            return None
        try:
            br = self.base_root()
            here = os.path.realpath(str(self.root))
            keys = Counter()
            for f in self.base_run(base_args, base_parse):
                k = str(key(f))
                keys[k.replace(os.path.realpath(br), here).replace(br, here)] += 1
            return keys
        except Exception as e:   # an unmeasurable base never narrows a lint: full mode
            self.force_full("base %s run failed (%s); full tree (fail closed)" % (self.base[:12], str(e)[:300]))
            return None

    # ------------------------------------------------------------------------------------------------ RULE P
    def tracked(self) -> frozenset:
        """Every git path tracked at HEAD or at base (exact bytes, surrogateescape)."""
        if self._tracked is None:
            names = set(_names(_git(self.root, "ls-files", "-z", raw=True)))
            if self.base:
                names |= set(_names(_git(self.root, "ls-tree", "-r", "-z", "--name-only", self.base, raw=True)))
            self._tracked = frozenset(names)
        return self._tracked

    def owner_of_text(self, finding, delim) -> tuple:
        """RULE P2 (Apollo r7 ruling on #7997): owner = the finding text up to the FIRST occurrence of the lint's
        declared owner delimiter `delim`. (owner, '') only when that one string is a tracked, plain path the PR did
        not change; else ('', why) -> the finding is the PR's. No candidate loop, no fallback to a shorter prefix
        (Prism bdcf0f713bf0: `old dir/app.py: ...` must never be owned by an unrelated tracked `old`). The one extra
        check only ever FAILS a finding: when the text starts with <a PR-changed path><delim> (`a:b.py:3:` with the PR
        editing `a:b.py`), it is the PR's, whatever the first-delimiter cut says."""
        s = str(finding)
        if not RULE_P:   # mutant: the lenient pre-r7 parse (first token)
            return lead_path(s), ""
        if not delim:
            return "", "the lint declares no owner delimiter"
        if any(s.startswith(c + delim) for c in self.changed):
            return "", "names a PR-changed path"
        i = s.find(delim)
        if i <= 0:
            return "", "no owner before delimiter %r" % delim
        owner = s[:i]
        if owner not in self.tracked():
            return "", "owner %r is not a tracked path" % owner
        if not plain(owner):
            return "", "owner %r is not a plain path" % owner
        return owner, ""

    def owner_paths(self, paths) -> tuple:
        """Structured paths (path_of): each must be a plain tracked path, else the finding is the PR's."""
        paths = tuple(p for p in paths if p)
        if not paths:
            return (), "names no path"
        if not RULE_P:
            return paths, ""
        tracked = self.tracked()
        for p in paths:
            if p not in tracked:
                return (), "%r is not a tracked path" % p
            if not plain(p):
                return (), "%r is not a plain path" % p
        return paths, ""

    @property
    def pr(self) -> bool:
        return self.mode == "pr"

    def force_full(self, reason):
        """Grade this run on the whole tree (fail closed) for a reason only the lint can see, e.g. a policy file it
        was pointed at that the base revision cannot supply."""
        if self.pr:
            self.mode, self.reason = "full", reason
            print(self.banner(), file=sys.stderr)

    def rule_s(self, paths, what="path") -> bool:
        """RULE S: every path this run reads or attributes must resolve with no symlink, or through unchanged
        in-tree relative symlinks (symlink_hazard). The first hazard grades the whole tree (reason printed); returns
        True when this run is (now) full. The one place every scoped read/attribution goes through."""
        if not self.pr or not RULE_S:
            return not self.pr
        for p in paths:
            why = (symlink_hazard(self.root, p, self.changed) if self.root is not None
                   else "%s cannot be resolved (no scan root)" % p) if p else ""
            if why:
                self.force_full("rule S: %s %s; full tree (fail closed)" % (what, why))
                return True
        return False

    def own_registry_rows(self, rel, rows_of):
        """A PR that edits a registry the lint reads (an allowlist, a ratchet baseline, cron/jobs.json) owns the
        findings on every path whose rows it changed, so dropping a row cannot hide the finding it exposes, and editing
        the registry does not drag the whole tree in. rows_of(text) -> {repo_path: comparable rows}; a missing file is
        ''. An unreadable base side makes this run full (fail closed). The registry path itself goes through RULE S
        whether or not the PR edits it: a linked registry the base worktree would resolve elsewhere grades full."""
        if not self.pr or self.rule_s([rel], "registry") or norm(rel) not in self.changed:
            return
        if os.path.islink(os.path.join(str(self.root), norm(rel))) or self._link_at_base(rel):
            # base `git show` yields the link TEXT, head reads the target: the two row sets are not comparable
            # (Prism 01cc3810e837). Grade full.
            self.mode, self.reason = "full", "%s is a symlink on base or head; full tree (fail closed)" % norm(rel)
            print(self.banner(), file=sys.stderr)
            return
        try:
            old = rows_of(_git(self.root, "show", "%s:%s" % (self.base, norm(rel))) if self._at_base(rel) else "")
            with open(os.path.join(str(self.root), norm(rel)), encoding="utf-8") as f:
                new = rows_of(f.read())
        except FileNotFoundError:
            new = rows_of("")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
            self.mode, self.reason = "full", "%s base/head unreadable (%s); full tree (fail closed)" % (rel, e)
            print(self.banner(), file=sys.stderr)
            return
        moved = {norm(p) for p in set(old) | set(new) if (p in old) != (p in new) or old.get(p) != new.get(p)}
        if moved:
            self.changed = self.changed | moved
            print("lint-scope: %s: %s edits rows of %d path(s); they count as changed: %s"
                  % (self.lint, norm(rel), len(moved), ", ".join(sorted(moved)[:8])), file=sys.stderr)

    def _link_at_base(self, rel) -> bool:
        try:
            out = _git(self.root, "ls-tree", self.base, "--", norm(rel))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False
        return out.startswith("120000 ")

    def _at_base(self, rel) -> bool:
        return subprocess.run(["git", "-C", str(self.root), "cat-file", "-e", "%s:%s" % (self.base, norm(rel))],
                              capture_output=True, env=_env()).returncode == 0

    def owns(self, *paths) -> bool:
        """True when a finding on any of `paths` fails this run (always, in full mode)."""
        if not self.pr:
            return True
        return any(p in self.changed for p in paths if p)

    def info(self, path, finding, out=None):
        self.info_count += 1
        print("INFO pre-existing on base, owner: %s: %s" % (path, str(finding).strip()), file=out or sys.stdout)

    def split(self, findings, path_of=None, text=str, out=None, base_args=None, base_parse=None, key=None,
              owner_delim=None):
        """The findings that FAIL this run, in order; the rest print as INFO lines (pr mode only).
        Owner: path_of(finding) -> a path or tuple of paths (structured, preferred), else text(finding) up to the
        first `owner_delim` (RULE P2, owner_of_text; the lint's documented output delimiter). Neither given: every
        finding is the PR's. Any finding without a plain tracked owner is the PR's (fails).
        text(finding) is the INFO line's message; key(finding) the fingerprint (default text).
        Base (RULE B): base_args(base_root) -> argv for BASE's own lint file run in the base worktree;
        base_parse(stdout) -> its findings in the same shape. A finding on unchanged paths passes only when the same
        fingerprint is on base (counted: two on head, one on base = one NEW). Base unmeasurable: full mode."""
        findings = list(findings)
        key = key or text
        resolved = []
        for f in findings:
            if not self.pr:
                resolved.append(((), ""))
            elif path_of is None:
                own, why = self.owner_of_text(text(f), owner_delim)
                resolved.append(((own,) if own else (), why))
            else:
                paths = path_of(f)
                resolved.append(self.owner_paths((paths,) if isinstance(paths, str) else tuple(paths or ())))
        # RULE S on every path a finding is attributed to (Prism 154114c47c47)
        self.rule_s([p for ps, _ in resolved for p in ps], "finding path")
        fail, cand = set(), []
        for i, (f, (paths, why)) in enumerate(zip(findings, resolved)):
            if not self.pr or not paths or self.owns(*paths):
                fail.add(i)   # the PR's own file, or no plain tracked owner (RULE P): it fails (closed)
            else:
                cand.append((i, f, paths))
        if cand:
            base = self._base_keys(base_args, base_parse, key)
            if base is None:          # unmeasurable base: every candidate fails (and the run is full)
                fail.update(i for i, _, _ in cand)
                cand = []
            for i, f, paths in cand:
                k = str(key(f))
                if base[k] > 0:
                    base[k] -= 1
                    self.info(paths[0], text(f), out=out)
                else:
                    fail.add(i)
                    print("lint-scope: %s: NEW vs base %s (named on unchanged %s): %s"
                          % (self.lint, self.base[:12], paths[0], str(text(f)).strip()[:300]), file=sys.stderr)
        return [f for i, f in enumerate(findings) if i in fail]

    def banner(self) -> str:
        if self.pr:
            return ("lint-scope: %s: pr mode vs base %s, %d changed file(s): only findings in them fail; others print "
                    "as INFO (kill switch LINT_SCOPE=full)" % (self.lint, self.base[:12], len(self.changed)))
        return "lint-scope: %s: full mode (%s)" % (self.lint, self.reason or "not a pull_request run")


def from_env(root, lint, inputs=(), full_if_deleted=False, quiet=False, out=None) -> Scope:
    """Resolve this run's scope for `lint` (a label for the banner). `inputs` = repo-relative paths whose change by
    the PR re-grades the whole tree (the lint's own file, its allowlist/baseline). Prints one banner line to `out`
    (default stderr, so a lint's stdout format is unchanged) unless quiet."""
    sc = _resolve(root, lint, inputs, full_if_deleted)
    sc.root = root
    if not quiet:
        print(sc.banner(), file=out or sys.stderr)
    return sc


def _resolve(root, lint, inputs, full_if_deleted) -> Scope:
    knob = (os.environ.get("LINT_SCOPE") or "").strip().lower()
    if knob == "full":
        return Scope(lint, "full", reason="LINT_SCOPE=full")
    forced = knob == "pr"
    if not forced and os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return Scope(lint, "full")
    base = (os.environ.get("LINT_SCOPE_BASE") or "").strip() or pr_base(root).strip()
    if not base or set(base) == {"0"}:
        return Scope(lint, "full", reason="no PR base known; full tree (fail closed)")
    try:
        top = _git(root, "rev-parse", "--show-toplevel").strip()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        top = ""
    if not top or os.path.realpath(top) != os.path.realpath(str(root)):
        # a fixture tree inside the checkout (or no repo at all): the PR diff does not describe it
        return Scope(lint, "full", reason="scan root is not a git toplevel; full tree")
    try:
        ensure_commit(root, base)
        raw = _raw_diff(root, base)
    except (OSError, RuntimeError, subprocess.SubprocessError, IndexError) as e:
        return Scope(lint, "full", base, reason="base %s unreadable (%s); full tree (fail closed)" % (base[:12], e))
    changed = [p for _, _, _, p in raw]
    deleted = {p for st, _, _, p in raw if st == "D"} if full_if_deleted else set()
    links = sorted(p for _, om, nm, p in raw if "120000" in (om, nm))
    if links and RULE_S:
        # RULE S: a PR that adds, removes, repoints or replaces a symlink can make head and base read different
        # bytes through the same path; the lint's tree walk cannot be told which reads went through it. Full tree.
        return Scope(lint, "full", base, changed,
                     reason="rule S: this PR changes symlink(s) %s; full tree (fail closed)" % ", ".join(links[:5]))
    # the listed inputs only (the lint file, its policy files, this helper). Not load-bearing under RULE B: a rule
    # change anywhere else shows up as a head-only finding; this just skips a base run that cannot narrow anything.
    closure = {norm(r) for r in (SELF_REL, *inputs) if r}
    touched = sorted(closure & set(changed))
    if touched:
        return Scope(lint, "full", base, changed,
                     reason="this PR changes the lint's own input(s) %s; full tree" % ", ".join(touched))
    if deleted:
        return Scope(lint, "full", base, changed,
                     reason="this PR deletes %d file(s) (%s); full tree" % (len(deleted), sorted(deleted)[0]))
    sc = Scope(lint, "pr", base, changed, root=root, lint_path=norm(inputs[0]) if inputs else "")
    for rel in sorted(closure):   # RULE S on the lint's listed inputs, every component (Prism 195976b88cbf)
        why = symlink_hazard(root, rel, changed) if RULE_S else ""
        if why:
            return Scope(lint, "full", base, changed, reason="rule S: input %s; full tree (fail closed)" % why)
    return sc

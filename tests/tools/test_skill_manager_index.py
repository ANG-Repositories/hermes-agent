"""_find_skill name index: one walk serves repeated lookups, and the index never hides a change.

The gateway profile showed ``_iter_skill_dirs`` (a full rglob over ~1.7k SKILL.md) at ~10% of GIL
samples because one skill_manage call resolved the same name up to four times. These tests pin
both halves: walk count (the perf contract) and freshness (external create/delete/move, categorized
names, external dirs, excluded paths).
"""

import shutil
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import tools.skill_manager_tool as smt


def _mk(root, rel, name=None):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or d.name}\ndescription: test skill\n---\n\n# x\n", encoding="utf-8")
    return d


@contextmanager
def _roots(local, *external):
    smt._invalidate_skill_index()
    with patch("tools.skill_manager_tool.SKILLS_DIR", local), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[local, *external]):
        yield
    smt._invalidate_skill_index()


@contextmanager
def _count_walks():
    calls = []
    real = smt._iter_skill_dirs

    def counting(root):
        calls.append(root)
        return real(root)

    with patch("tools.skill_manager_tool._iter_skill_dirs", side_effect=counting):
        yield calls


def test_repeated_lookups_walk_once(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "devops/alpha")
    with _roots(local), _count_walks() as walks:
        for _ in range(4):  # one skill_manage call resolves the name up to four times
            assert smt._find_skill("alpha")["path"] == local / "devops" / "alpha"
        assert len(walks) == 1


def test_external_create_is_seen_on_next_lookup(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "devops/alpha")
    with _roots(local):
        assert smt._find_skill("alpha")
        assert smt._find_skill("beta") is None
        _mk(local, "devops/beta")  # another process creates it
        assert smt._find_skill("beta")["path"] == local / "devops" / "beta"


def test_external_delete_is_seen_on_next_lookup(tmp_path):
    local = tmp_path / "skills"
    d = _mk(local, "devops/alpha")
    with _roots(local):
        assert smt._find_skill("alpha")
        shutil.rmtree(d)
        assert smt._find_skill("alpha") is None


def test_external_move_is_seen_on_next_lookup(tmp_path):
    local = tmp_path / "skills"
    d = _mk(local, "devops/alpha")
    with _roots(local):
        assert smt._find_skill("alpha")["path"] == d
        (local / "mlops").mkdir()
        d.rename(local / "mlops" / "alpha")  # recategorized by a git pull
        assert smt._find_skill("alpha")["path"] == local / "mlops" / "alpha"
        assert smt._find_skill("mlops/alpha")["path"] == local / "mlops" / "alpha"
        assert smt._find_skill("devops/alpha") is None


def test_external_rename_old_name_misses_new_name_hits(tmp_path):
    local = tmp_path / "skills"
    d = _mk(local, "devops/alpha")
    with _roots(local):
        assert smt._find_skill("alpha")
        d.rename(local / "devops" / "gamma")
        assert smt._find_skill("alpha") is None
        assert smt._find_skill("gamma")["path"] == local / "devops" / "gamma"


def test_categorized_and_bare_names_resolve(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "mlops/axolotl")
    with _roots(local):
        assert smt._find_skill("mlops/axolotl")["path"] == local / "mlops" / "axolotl"
        assert smt._find_skill("axolotl")["path"] == local / "mlops" / "axolotl"
        assert smt._find_skill("devops/axolotl") is None


def test_external_dirs_resolve_and_local_wins(tmp_path):
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(ext, "only-ext")
    _mk(ext, "both")
    _mk(local, "cat/both")
    with _roots(local, ext):
        assert smt._find_skill("only-ext")["path"] == ext / "only-ext"
        assert smt._find_skill("both")["path"] == local / "cat" / "both"
        # categorized form matches relative to the LOCAL root only
        assert smt._find_skill("devops/only-ext") is None


def test_excluded_paths_stay_excluded(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "devops/alpha")
    _mk(local, ".git/hidden")                 # VCS dir
    _mk(local, "devops/alpha/references/sub")  # support dir of a skill root
    with _roots(local):
        assert smt._find_skill("hidden") is None
        assert smt._find_skill("sub") is None
        assert smt._find_skill("alpha")


def test_own_create_invalidates_shadowed_index(tmp_path):
    """A same-named skill appearing EARLIER in scan order is invisible to hit validation; the
    in-process generation bump (create/delete through skill_manage) must drop the index."""
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(ext, "dup")
    with _roots(local, ext):
        assert smt._find_skill("dup")["path"] == ext / "dup"
        _mk(local, "cat/dup")
        smt._invalidate_skill_index()
        assert smt._find_skill("dup")["path"] == local / "cat" / "dup"


def test_mutating_call_sees_external_local_override_within_ttl(tmp_path):
    """Prism b31279330fb5: another process creates a local override of a cached external skill.
    Hit validation cannot see it (the external path is still valid), so a mutating skill_manage
    call must drop the index before its first lookup instead of waiting out the TTL."""
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(ext, "dup")
    with _roots(local, ext), patch("tools.skill_manager_tool.HERMES_HOME", tmp_path), \
         patch("tools.skill_manager_tool._apply_skill_write_gate", return_value=None):
        assert smt._find_skill("dup")["path"] == ext / "dup"   # cached: external only
        d = _mk(local, "cat/dup")                                # external process, no generation bump
        (d / "SKILL.md").write_text(
            "---\nname: dup\ndescription: Use when testing. Does a thing.\n---\n\n# Dup\n\nold line\n",
            encoding="utf-8")
        ext_before = (ext / "dup" / "SKILL.md").read_text(encoding="utf-8")
        seen = []
        real_find = smt._find_skill

        def spy(name):
            r = real_find(name)
            seen.append(r["path"] if r else None)
            return r

        with patch("tools.skill_manager_tool._find_skill", side_effect=spy):
            smt.skill_manage(action="patch", name="dup", old_string="old line", new_string="new line")
        assert seen and all(p == local / "cat" / "dup" for p in seen), seen
        assert (ext / "dup" / "SKILL.md").read_text(encoding="utf-8") == ext_before


def test_repeated_missing_lookups_walk_once(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "nested/category/alpha")
    with _roots(local), _count_walks() as walks:
        for _ in range(50):
            assert smt._find_skill("absent") is None
        assert len(walks) == 1
        _mk(local, "nested/category/absent")
        assert smt._find_skill("absent")["path"] == local / "nested/category/absent"
        assert len(walks) == 2


def test_invalidated_build_is_never_returned(tmp_path):
    local = tmp_path / "skills"
    d = _mk(local, "cat/alpha")
    real = smt._build_skill_index
    builds = []

    def racing(roots):
        index = real(roots)
        builds.append(index)
        if len(builds) == 1:
            d.rename(local / "cat/beta")
            smt._invalidate_skill_index()
        return index

    with _roots(local), patch.object(smt, "_build_skill_index", side_effect=racing):
        assert smt._find_skill("alpha") is None
        assert smt._find_skill("beta")["path"] == local / "cat/beta"
        assert len(builds) == 2


def test_external_shadow_is_visible_without_ttl(tmp_path):
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(ext, "dup")
    with _roots(local, ext):
        assert smt._find_skill("dup")["path"] == ext / "dup"
        _mk(local, "cat/dup")
        assert smt._find_skill("dup")["path"] == local / "cat/dup"


def test_skill_manage_patch_walks_at_most_once(tmp_path):
    """End-to-end: a patch resolves the name in preflight, ledger, _locate_for_write and
    _record_success; with the index that is one walk, not four."""
    local = tmp_path / "skills"
    d = _mk(local, "devops/alpha")
    (d / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Use when testing. Does a thing.\n---\n\n# Alpha\n\nold line\n",
        encoding="utf-8")
    with _roots(local), patch("tools.skill_manager_tool.HERMES_HOME", tmp_path), \
         patch("tools.skill_manager_tool._apply_skill_write_gate", return_value=None), \
         _count_walks() as walks, \
         patch.object(smt, "_build_skill_index", wraps=smt._build_skill_index) as builds, \
         patch.object(smt, "_find_skill_indexed", wraps=smt._find_skill_indexed) as resolve:
        out = smt.skill_manage(action="patch", name="alpha", old_string="old line", new_string="new line")
        assert '"success": true' in out, out
        assert "new line" in (d / "SKILL.md").read_text(encoding="utf-8")
        assert len(walks) == 1, walks
        out = smt.skill_manage(action="patch", name="alpha", old_string="new line", new_string="old line")
        assert '"success": true' in out, out
        assert len(walks) == 1, walks
        assert resolve.call_count == 2  # exactly one resolution per public operation
        assert builds.call_count == 1  # atomic same-path replacement never rebuilds


def test_restored_category_permissions_reselect_local_override(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("requires permission enforcement for a non-root process")
    local, external = tmp_path / "skills", tmp_path / "external"
    local_skill = _mk(local, "category/alpha")
    external_skill = _mk(external, "alpha")
    category = local_skill.parent
    category.chmod(0)
    try:
        with _roots(local, external):
            assert smt._find_skill("alpha")["path"] == external_skill
            category.chmod(0o755)
            assert smt._find_skill("alpha")["path"] == local_skill
    finally:
        category.chmod(0o755)


def test_thread_invalidation_discards_inflight_build(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    local = tmp_path / "skills"
    d = _mk(local, "cat/alpha")
    built, release = threading.Event(), threading.Event()
    real = smt._build_skill_index
    count = 0

    def blocked(roots):
        nonlocal count
        index = real(roots)
        count += 1
        if count == 1:
            built.set()
            assert release.wait(10)
        return index

    with _roots(local), patch.object(smt, "_build_skill_index", side_effect=blocked), \
         ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(smt._find_skill, "alpha")
        try:
            assert built.wait(10)
            d.rename(local / "cat/beta")
            smt._invalidate_skill_index()
        finally:
            release.set()
        assert future.result(timeout=10) is None
        assert smt._find_skill("beta")["path"] == local / "cat/beta"
        assert count == 2


def test_concurrent_builders_share_one_walk(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    local = tmp_path / "skills"
    _mk(local, "cat/alpha")
    barrier = threading.Barrier(8)

    def lookup(_):
        barrier.wait(timeout=10)
        return smt._find_skill("alpha")

    with _roots(local), _count_walks() as walks, ThreadPoolExecutor(max_workers=8) as pool:
        assert all(r["path"] == local / "cat/alpha" for r in pool.map(lookup, range(8)))
        assert len(walks) == 1


@pytest.mark.parametrize("root_kind", ["local", "external"])
def test_external_nested_empty_category_create(tmp_path, root_kind):
    local, ext = tmp_path / "skills", tmp_path / "ext"
    root = local if root_kind == "local" else ext
    (root / "deep/empty").mkdir(parents=True)
    with _roots(local, ext), _count_walks() as walks:
        assert smt._find_skill("beta") is None
        _mk(root, "deep/empty/beta")
        assert smt._find_skill("beta")["path"] == root / "deep/empty/beta"


@pytest.mark.parametrize("n", [2000])
def test_benchmark_2000_skills_warm_lookup_skips_walk(tmp_path, n):
    local = tmp_path / "skills"
    for i in range(n):
        _mk(local, f"cat{i % 40}/skill-{i}")
    with _roots(local), _count_walks() as walks:
        smt._find_skill("skill-1999")
        t0 = time.perf_counter()
        for i in range(200):
            assert smt._find_skill(f"skill-{i}")
        warm = (time.perf_counter() - t0) / 200
        assert len(walks) == 1
    print(f"\n2000-skill warm _find_skill: {warm * 1e6:.1f} us/call")

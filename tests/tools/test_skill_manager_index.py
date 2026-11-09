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


def test_ttl_bounds_shadow_staleness(tmp_path):
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(ext, "dup")
    with _roots(local, ext):
        assert smt._find_skill("dup")["path"] == ext / "dup"
        _mk(local, "cat/dup")
        real = time.monotonic
        with patch("time.monotonic", lambda: real() + smt._SKILL_INDEX_TTL_SECONDS + 1):
            assert smt._find_skill("dup")["path"] == local / "cat" / "dup"


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
         _count_walks() as walks:
        out = smt.skill_manage(action="patch", name="alpha", old_string="old line", new_string="new line")
        assert '"success": true' in out, out
        assert "new line" in (d / "SKILL.md").read_text(encoding="utf-8")
        assert len(walks) <= 1, walks


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

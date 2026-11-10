"""Public-entrypoint discriminating probes for Prism findings at fc3b8e82."""
import json
import os
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
import pytest
from tools import skill_manager_tool as smt
from tests.tools.test_skill_manager_index import _mk, _roots


@pytest.fixture(autouse=True, params=['head', 'main-rglob-control'])
def discovery_implementation(request):
    if request.param == 'head':
        yield
    else:
        import types
        from tests.tools import review171_legacy_lookup as legacy
        lookup = types.FunctionType(legacy._find_skill.__code__, smt.__dict__)
        with patch.object(smt, '_find_skill', lookup):
            yield


def env(stack, home, root):
    (root / '.locks').mkdir(exist_ok=True)  # avoid the public mutation lock masking the race
    stack.enter_context(_roots(root))
    for symbol, value in [('HERMES_HOME', home), ('_apply_skill_write_gate', None),
                          ('_background_review_preflight', None), ('_record_success', None),
                          ('_find_skill_in_other_profiles', [])]:
        stack.enter_context(patch.object(smt, symbol, home) if symbol == 'HERMES_HOME'
                            else patch.object(smt, symbol, return_value=value))


def public(name, action='unknown'):
    return json.loads(smt.skill_manage(action=action, name=name,
                    old_string='# x', new_string='# changed'))


@pytest.mark.parametrize('modeled_order', [False, True], ids=['native-fs', 'insertion-ordered-fs'])
def test_97e9a72be65e_replaced_tree_precedence(tmp_path, modeled_order):
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'); _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        if modeled_order:
            real_scandir = os.scandir
            class Entries:
                def __init__(self, entries): self.entries = iter(entries)
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def __iter__(self): return self
                def __next__(self): return next(self.entries)
                def close(self): pass
            def insertion_scandir(path):
                with real_scandir(path) as entries:
                    values = list(entries)
                # Model the legal filesystem ordering change after the inode swap.
                reverse = (tmp_path / 'old-tree').exists()
                return Entries(sorted(values, key=lambda e: e.name, reverse=reverse))
            stack.enter_context(patch('os.scandir', side_effect=insertion_scandir))
        public('foo')  # public lookup warms the cache without changing either skill
        original = next(smt._iter_skill_dirs(root)).relative_to(root)
        replacement = tmp_path / 'replacement'
        # Reverse the actual discovery winner, not an assumed filesystem ordering.
        order = ['b/foo', 'a/foo'] if str(original) == 'a/foo' else ['a/foo', 'b/foo']
        for rel in order:
            _mk(replacement, rel)
        (replacement / '.locks').mkdir(exist_ok=True)
        root.rename(tmp_path / 'old-tree')
        replacement.rename(root)
        fresh = next(smt._iter_skill_dirs(root))
        if fresh.relative_to(root) == original:
            pytest.skip('filesystem does not expose reverse insertion order')
        result = public('foo', 'patch')
        assert result.get('success'), result
        assert '# changed' in (fresh / 'SKILL.md').read_text(encoding="utf-8"), (original, fresh, result)


def test_6a726bb2ae97_directory_created_during_walk(tmp_path, request):
    root = tmp_path / 'skills'
    _mk(root, 'alpha')
    real_walk = os.walk
    raced = False
    def racing_walk(top, *args, **kwargs):
        nonlocal raced
        for base, dirs, files in real_walk(top, *args, **kwargs):
            if Path(base) == root and not raced:
                raced = True
                _mk(root, 'new')  # after enumeration, before stamp sampled in caller
            yield base, dirs, files
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        with patch('os.walk', side_effect=racing_walk):
            public('new')
        if not raced and request.node.callspec.params['discovery_implementation'] == 'main-rglob-control':
            _mk(root, 'new')  # legacy discovery has no os.walk snapshot seam
        else:
            assert raced
        added = _mk(root, 'new/another')
        assert added in list(smt._iter_skill_dirs(root)), 'fresh pre-PR discovery must see it'
        result = public('another', 'patch')
        assert result.get('success'), result
        assert '# changed' in (added / 'SKILL.md').read_text(encoding="utf-8")


def test_preflight_rejection_does_not_discover(tmp_path):
    root = tmp_path / 'skills'
    _mk(root, 'alpha')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch.object(smt, '_find_skill_indexed',
                                        side_effect=AssertionError('must not discover')))
        stack.enter_context(patch.object(smt, '_background_review_preflight',
                                        return_value={'success': False, 'error': 'rejected'}))
        assert public('alpha', 'patch') == {'success': False, 'error': 'rejected'}


def test_unstable_tree_returns_structured_error(tmp_path, request):
    if request.node.callspec.params['discovery_implementation'] != 'head':
        pytest.skip('legacy lookup has no snapshot retries')
    root = tmp_path / 'skills'
    _mk(root, 'alpha')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch.object(smt, '_index_tree_unchanged', return_value=False))
        builds = stack.enter_context(patch.object(smt, '_build_skill_index',
                                                 wraps=smt._build_skill_index))
        result = public('alpha', 'patch')
        assert result['success'] is False
        assert 'changed repeatedly' in result['error']
        assert builds.call_count == 3
        assert smt._SKILL_OPERATION_TARGETS.get() is None



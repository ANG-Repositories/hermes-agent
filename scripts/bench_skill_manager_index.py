#!/usr/bin/env python3
# SCHEDULE: manual (fixture benchmark invoked by implementer/reviewer)
"""Fixture-only public skill_manage benchmark; pass pre-index source via --baseline.

Both versions run identical real patch/guard/atomic-write dispatch. Approval staging, sync,
ledger, lifecycle telemetry and cross-profile diagnostics are isolated from the host. Cold
means a cold path index, not cold Python imports. Walk counts are the deterministic gate;
wall times include filesystem writes and are not a production latency estimate.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools.skill_manager_tool as smt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    tree = ast.parse(args.baseline.read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_find_skill')
    scope = dict(vars(smt))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(args.baseline), 'exec'), scope)
    original_find = scope['_find_skill']
    dispatch = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'skill_manage')
    dispatch.decorator_list = []
    exec(compile(ast.Module(body=[dispatch], type_ignores=[]), str(args.baseline), 'exec'), scope)
    original_dispatch = scope['skill_manage']
    with tempfile.TemporaryDirectory(prefix='skill-index-bench-') as td:
        home = Path(td)
        root = home / 'skills'
        for i in range(2000):
            directory = root / f'cat{i % 40}' / f'skill-{i}'
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(
                f'---\nname: skill-{i}\ndescription: Use when testing. Does a thing.\n---\n\n# Skill\n\nold line\n',
                encoding='utf-8')
        # Use the last discovered target, independent of filesystem directory ordering.
        target = list(smt._iter_skill_dirs(root))[-1] / 'SKILL.md'
        with patch.object(smt, 'HERMES_HOME', home), patch.object(smt, 'SKILLS_DIR', root), \
             patch('agent.skill_utils.get_all_skills_dirs', return_value=[root]), \
             patch.object(smt, '_apply_skill_write_gate', return_value=None), \
             patch.object(smt, '_find_skill_in_other_profiles', return_value=[]), \
             patch('tools.skill_ledger.ledger_enabled', return_value=False), \
             patch('tools.skill_usage._emit_skill_lifecycle'), \
             patch.object(smt, '_maybe_debounced_sync_push'):
            # Load the same lazy imports for BOTH cases outside the measurement.
            for label in ('before', 'after'):
                text = target.read_text(encoding='utf-8-sig')
                target.write_text(text.replace('new line', 'old line'), encoding='utf-8')
                call = original_dispatch if label == 'before' else smt.skill_manage
                # Baseline dispatch shares patched side-effect seams, but keeps its actual
                # pre-index entrypoint (no extra eager lookup introduced by this PR).
                scope.update({k: v for k, v in vars(smt).items()
                              if k not in {'_find_skill', 'skill_manage'}})
                finder = original_find if label == 'before' else smt._find_skill
                with patch.object(smt, '_find_skill', finder):
                    counter = 0

                    def invoke(missing=False):
                        nonlocal counter
                        old, new = ('old line', 'new line') if counter % 2 == 0 else ('new line', 'old line')
                        if not missing:
                            counter += 1
                        return json.loads(call(action='patch', name='absent' if missing else target.parent.name,
                                               old_string=old, new_string=new))

                    assert invoke()['success']
                    smt._invalidate_skill_index()
                    real_walk = smt._iter_skill_dirs
                    with patch.object(smt, '_iter_skill_dirs', wraps=real_walk) as walks, \
                         patch.object(smt, '_build_skill_index', wraps=smt._build_skill_index) as builds, \
                         patch('os.walk', wraps=os.walk) as topology_walks:
                        if label == 'before':
                            original_find.__globals__['_iter_skill_dirs'] = walks
                        t = time.perf_counter()
                        assert invoke()['success']
                        cold = time.perf_counter() - t
                        cold_walks = walks.call_count
                        cold_topology_walks = topology_walks.call_count
                        t = time.perf_counter()
                        for _ in range(20):
                            assert invoke()['success']
                        warm = (time.perf_counter() - t) / 20
                        warm_walks = walks.call_count - cold_walks
                        warm_topology_walks = topology_walks.call_count - cold_topology_walks
                        t = time.perf_counter()
                        for _ in range(20):
                            assert not invoke(True)['success']
                        negative = (time.perf_counter() - t) / 20
                        negative_walks = walks.call_count - cold_walks - warm_walks
                        negative_topology_walks = topology_walks.call_count - cold_topology_walks - warm_topology_walks
                        if label == 'after':
                            assert warm_topology_walks == negative_topology_walks == 0
                            assert cold_walks == 1 and warm_walks == negative_walks == 0
                            assert builds.call_count == 1
                        print(json.dumps(dict(version=label, skills=2000, cold_ms=cold * 1000,
                                              repeated_ms=warm * 1000, negative_ms=negative * 1000,
                                              cold_os_walks=cold_topology_walks,
                                              repeated_os_walks=warm_topology_walks,
                                              negative_os_walks=negative_topology_walks,
                                              cold_walks=cold_walks, repeated_walks=warm_walks,
                                              negative_walks=negative_walks, builds=builds.call_count)))


if __name__ == '__main__':
    main()

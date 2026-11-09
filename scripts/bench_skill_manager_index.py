#!/usr/bin/env python3
# SCHEDULE: manual (fixture benchmark invoked by implementer/reviewer)
"""Fixture-only public skill_manage benchmark; pass pre-index source via --baseline."""
import argparse
import ast
import json
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
    tree = ast.parse(args.baseline.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_find_skill')
    scope = dict(vars(smt))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(args.baseline), 'exec'), scope)
    original_find = scope['_find_skill']
    with tempfile.TemporaryDirectory(prefix='skill-index-bench-') as td:
        home = Path(td)
        root = home / 'skills'
        for i in range(2000):
            directory = root / f'cat{i % 40}' / f'skill-{i}'
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(
                f'---\nname: skill-{i}\ndescription: Use when testing. Does a thing.\n---\n\n# Skill\n\nold line\n')
        with patch.object(smt, 'HERMES_HOME', home), patch.object(smt, 'SKILLS_DIR', root), \
             patch('agent.skill_utils.get_all_skills_dirs', return_value=[root]), \
             patch.object(smt, '_apply_skill_write_gate', return_value=None), \
             patch.object(smt, '_find_skill_in_other_profiles', return_value=[]), \
             patch.object(smt, '_maybe_debounced_sync_push'):
            for label in ('before', 'after'):
                target = root / 'cat39/skill-1999/SKILL.md'
                target.write_text(target.read_text().replace('new line', 'old line'))
                smt._invalidate_skill_index()
                # Pre-index public dispatch had no operation binding. Reuse the actual original
                # finder in every downstream caller, not a synthetic timing approximation.
                call = smt.skill_manage.__wrapped__ if label == 'before' else smt.skill_manage
                finder = original_find if label == 'before' else smt._find_skill
                with patch.object(smt, '_find_skill', finder):
                    counter = 0

                    def invoke(missing=False):
                        nonlocal counter
                        old, new = ('old line', 'new line') if counter % 2 == 0 else ('new line', 'old line')
                        if not missing:
                            counter += 1
                        return json.loads(call(action='patch', name='absent' if missing else 'skill-1999',
                                               old_string=old, new_string=new))
                    t = time.perf_counter()
                    first = invoke()
                    assert first['success'], first
                    cold = time.perf_counter() - t
                    t = time.perf_counter()
                    for _ in range(20):
                        assert invoke()['success']
                    warm = (time.perf_counter() - t) / 20
                    t = time.perf_counter()
                    for _ in range(20):
                        assert not invoke(True)['success']
                    negative = (time.perf_counter() - t) / 20
                    print(json.dumps(dict(version=label, skills=2000, cold_ms=cold * 1000,
                                          repeated_ms=warm * 1000, negative_ms=negative * 1000)))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# SCHEDULE: manual (PR/CI byte-parity check; caller supplies the pinned canonical + vendored registry files)
"""Compare canonical and vendored registries as bytes, and pinned vendored cores by sha256; missing inputs fail closed.

Usage: lint-registry-parity.py CANONICAL_REGISTRY VENDORED_REGISTRY [VENDORED_REGISTRY ...]
                               [--vendored-core PATH=SHA256 ...]
CANONICAL is the hermes-home fleet/admission-priority.json at the approved main SHA
(in a consumer repo: the PIN.json-pinned copy under .github/router-core/), not a cached
local configuration. No network fetch, normalization, or mutable home-path default.
--vendored-core: a consumer's copy of a shared core file (admission.py, admission_demand.py,
alloc_client.py, ...) must hash to the pinned sha256. Checked on EVERY run, whatever the
diff touched: drift that is already on the base branch is red too.
"""
import argparse
import hashlib
from pathlib import Path
import re
import sys


def check(paths):
    if len(paths) < 2:
        return ['registry-parity: a canonical and at least one vendored registry are required']
    rows, errors = [], []
    for path in paths:
        try:
            rows.append((path, Path(path).read_bytes()))
        except OSError as exc:
            errors.append(f'{path}: registry unreadable: {exc}')
    if errors:
        return errors
    canonical = rows[0][1]
    return [f'{path}: registry differs from {rows[0][0]} '
            f'(sha256={hashlib.sha256(raw).hexdigest()})'
            for path, raw in rows[1:] if raw != canonical]


def check_cores(specs):
    errors = []
    for spec in specs:
        path, sep, want = spec.rpartition('=')
        if not sep or not path or not re.fullmatch(r'[0-9a-f]{64}', want):
            errors.append(f'--vendored-core wants PATH=SHA256 (64 lowercase hex), got {spec!r}')
            continue
        try:
            got = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f'{path}: vendored core unreadable: {exc}')
            continue
        if got != want:
            errors.append(f'{path}: vendored-core-drift: sha256 {got} != pinned {want}; '
                          f're-copy the shared core and re-pin')
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('registry', type=Path, nargs='+')
    ap.add_argument('--vendored-core', action='append', default=[], metavar='PATH=SHA256')
    a = ap.parse_args(argv)
    errors = check(a.registry) + check_cores(a.vendored_core)
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())

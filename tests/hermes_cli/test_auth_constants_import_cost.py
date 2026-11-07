"""Importing ``hermes_cli.auth_constants`` must not spawn processes (t_a1bd70f8).

Its module-scope ``CODEX_OAUTH_USER_AGENT`` called ``get_version_info()``, which on an
unstamped checkout runs ~8 git subprocesses. Every importer paid it: on the 4-vCPU CI guest
~1.7 s per fresh process, which pushed the clean-graph import smoke past its 180 s wall.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
calls = []
_real_init = subprocess.Popen.__init__
def _spy(self, args, *a, **kw):
    calls.append(args if isinstance(args, str) else list(map(str, args)))
    return _real_init(self, args, *a, **kw)
subprocess.Popen.__init__ = _spy
import hermes_cli.auth_constants as ac
import hermes_cli.auth_codex  # the one in-tree consumer
print(json.dumps({"at_import": list(calls)}), flush=True)
ua = ac.codex_oauth_user_agent()
print(json.dumps({"ua": ua, "legacy": ac.CODEX_OAUTH_USER_AGENT}))
"""


def test_import_spawns_no_subprocess_and_user_agent_resolves_on_use():
    cp = subprocess.run([sys.executable, "-c", _PROBE, str(REPO)], capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=120, cwd=str(REPO))
    lines = cp.stdout.strip().splitlines()
    assert lines, cp.stderr[-3000:]
    at_import = json.loads(lines[0])["at_import"]
    assert at_import == [], f"import spawned {len(at_import)} process(es): {at_import[:5]}"
    assert cp.returncode == 0, cp.stderr[-3000:]
    out = json.loads(lines[-1])
    assert out["ua"].startswith("hermes-cli/") and len(out["ua"]) > len("hermes-cli/")
    assert out["legacy"] == out["ua"]

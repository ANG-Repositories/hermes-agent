"""An inherited PLACEMENT_HOME never beats a test's own HERMES_HOME (t_dbacd1f2).

``hermes_cli.fleet_placement`` resolves its state root from PLACEMENT_HOME before the kanban root, so a pytest run
that inherited ``PLACEMENT_HOME=~/.hermes`` (test-gate, 2026-11-06 18:25) wrote and unlinked the LIVE
``config/fleet-hosts.json``. The conftest drops PLACEMENT_HOME per test; tests of PLACEMENT_HOME precedence set it.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INNER = "tests/hermes_cli/test_placement_home_hermeticity.py::test_fleet_root_is_the_test_sandbox"
_INHERITED = os.environ.get("PLACEMENT_HOME")


def test_fleet_root_is_the_test_sandbox():
    from hermes_cli import fleet_placement as fp

    assert "PLACEMENT_HOME" not in os.environ, f"inherited PLACEMENT_HOME={_INHERITED} reached the test"
    root = fp.placement.fleet_root().resolve()
    home = Path(os.environ["HERMES_HOME"]).resolve()
    assert root == home or home in root.parents, f"fleet_root {root} is outside the test HERMES_HOME {home}"
    assert fp.admission_demand._root().resolve() == root
    if _INHERITED:
        assert root != Path(_INHERITED).resolve()


def test_exported_placement_home_does_not_reach_a_test(tmp_path):
    """E2E: run the check above in a child pytest that inherits PLACEMENT_HOME pointing at a populated root."""
    live = tmp_path / "live"
    (live / "config").mkdir(parents=True)
    hosts = live / "config" / "fleet-hosts.json"
    hosts.write_text('{"sentinel": true}\n')
    env = dict(os.environ)
    for key in ("HERMES_HOME", "PYTEST_ADDOPTS", "PYTEST_DEBUG_TEMPROOT"):
        env.pop(key, None)
    env["PLACEMENT_HOME"] = str(live)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", INNER],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert hosts.read_text() == '{"sentinel": true}\n'
    assert sorted(p.name for p in live.rglob("*")) == ["config", "fleet-hosts.json"]

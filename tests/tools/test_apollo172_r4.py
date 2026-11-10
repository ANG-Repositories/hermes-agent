"""Operator probes for Prism findings on PR 172 at ad5990f5: exported shell options must reach
the command's bash exactly as they did when Popen ran bash directly (main). Run on both spawn
paths: the no-fork one (forced, as on macOS) and plain Popen. The OLDPWD probe failed on main
and head alike; OLDPWD parity is covered by test_spawn_parity.py instead."""
import pytest

import tools.spawn_nofork as nofork
from tools.environments.local import LocalEnvironment

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture(params=["nofork", "plain-popen"])
def env(tmp_path, monkeypatch, request):
    monkeypatch.setattr(nofork, "_FORK_COPIES_PARENT", request.param == "nofork")
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    environment = LocalEnvironment(cwd=str(work), timeout=30)
    yield environment
    environment.cleanup()


def test_exported_shellopts_pipefail_reaches_the_command(env):
    """c7096b81b7d0 at ad5990f5: SHELLOPTS=pipefail in the run environment."""
    baseline = env.execute("false | true")
    assert baseline["returncode"] == 0, baseline  # without pipefail the pipeline is green
    env.env["SHELLOPTS"] = "pipefail"
    result = env.execute("false | true")
    assert result["returncode"] == 1, result


def test_exported_bashopts_reaches_the_command(env):
    env.env["BASHOPTS"] = "nullglob"
    result = env.execute("shopt -q nullglob && echo on || echo off")
    assert result["output"].strip().splitlines()[-1] == "on", result

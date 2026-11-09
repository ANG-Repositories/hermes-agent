"""Terminal-tool spawns must not fork the gateway (t_afc525a2).

``Popen(cwd=..., start_new_session=True)`` and the default ``close_fds=True`` send CPython
to ``_posixsubprocess.fork_exec``, which on macOS forks the whole (6 GB) gateway with the
GIL held. These tests make that path raise and require the terminal tool and the shell
lint to keep working, with the same cwd / env / stdin / session / fd / timeout / group-kill
semantics. Real subprocesses throughout: a mock cannot show group membership or fd
inheritance. The GIL-hold numbers come from ``tests/tools/spawn_gil_bench.py``.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

import tools.spawn_nofork as nofork
from tools.environments.local import LocalEnvironment
from tools.file_operations_lint_shell import _parse_check

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def no_fork_exec(monkeypatch):
    """Make CPython's fork path fail loudly; posix_spawn stays available."""
    import _posixsubprocess

    def _forbidden(*_a, **_k):
        raise AssertionError("fork_exec called: this spawn forks the whole parent under the GIL")

    monkeypatch.setattr(_posixsubprocess, "fork_exec", _forbidden)
    if hasattr(subprocess, "_fork_exec"):  # 3.12+ binds it at import
        monkeypatch.setattr(subprocess, "_fork_exec", _forbidden)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # an unreaped zombie still answers kill(0)
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return True


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "work dir"
    work.mkdir()
    environment = LocalEnvironment(cwd=str(work), timeout=30)
    yield environment
    environment.cleanup()


def test_execute_does_not_fork_and_keeps_cwd_env_stdin(no_fork_exec, env, tmp_path):
    env.env["T_AFC525A2_MARK"] = "ok-env"
    result = env.execute('pwd -P; echo "$T_AFC525A2_MARK"; cat')
    assert result["returncode"] == 0, result
    lines = result["output"].strip().splitlines()
    assert lines[0] == os.path.realpath(tmp_path / "work dir")
    assert lines[1] == "ok-env"

    piped = env.execute("cat", stdin_data="line one\nline two")
    assert piped["returncode"] == 0, piped
    assert piped["output"].strip() == "line one\nline two"


def test_run_bash_leads_new_session_in_cwd(no_fork_exec, env, tmp_path):
    proc = env._run_bash('echo "$$"; pwd -P; sleep 30')
    try:
        assert int(proc.stdout.readline()) == proc.pid, "bash must be the spawned pid (exec, no extra child)"
        assert proc.stdout.readline().strip() == os.path.realpath(tmp_path / "work dir")
        assert os.getpgid(proc.pid) == proc.pid != os.getpgrp()
        if nofork._session_mode == "setsid":
            assert os.getsid(proc.pid) == proc.pid
    finally:
        env._kill_process(proc)


def test_kill_process_takes_down_grandchildren(no_fork_exec, env):
    proc = env._run_bash("sleep 300 & echo $!; sleep 300")
    grandchild = int(proc.stdout.readline())
    assert _alive(grandchild)
    env._kill_process(proc)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild), "process-group kill left the grandchild running"
    assert proc.poll() is not None


def test_timeout_returns_124_and_reaps_the_group(no_fork_exec, env, tmp_path):
    marker = tmp_path / "gc.pid"
    started = time.monotonic()
    result = env.execute(f"sleep 300 & echo $! > '{marker}'; sleep 300", timeout=2)
    assert result["returncode"] == 124, result
    assert time.monotonic() - started < 20
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_missing_cwd_fails_inside_the_child_not_the_gateway(no_fork_exec, env, tmp_path):
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'cd -- "$1" || exit 126; shift; exec "$@"', "sh",
                               str(tmp_path / "gone"), "/bin/echo", "ran"],
                              new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 126 and "ran" not in out


def test_inheritable_fds_are_not_passed_to_the_child(no_fork_exec, tmp_path):
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)  # a leak close_fds=True used to stop
    try:
        proc = nofork.spawn_popen(
            ["/bin/sh", "-c", f'[ -e /dev/fd/{write_end} ] && echo leaked || echo closed'],
            stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate(timeout=10)
    finally:
        os.close(read_end)
        os.close(write_end)
    assert out.strip() == "closed"


def test_parse_check_does_not_fork(no_fork_exec):
    assert _parse_check("echo ok\n", "bash")[0] is True
    ok, msg = _parse_check("if then fi (\n", "bash")
    assert ok is False and "syntax error" in msg


def test_run_nofork_timeout_kills_child(no_fork_exec):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        nofork.run_nofork(["/bin/sleep", "30"], stdout=subprocess.PIPE, timeout=0.5)
    assert time.monotonic() - started < 10


def test_setpgroup_when_platform_lacks_setsid(no_fork_exec, monkeypatch):
    real = os.posix_spawn

    def no_setsid(*args, **kwargs):
        if kwargs.get("setsid"):
            raise NotImplementedError("setsid")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawn", no_setsid)
    monkeypatch.setattr(nofork, "_session_mode", "setsid")
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'echo "$$ $(ps -o pgid= -p $$)"'],
                              new_session=True, stdout=subprocess.PIPE, text=True)
    pid, pgid = (int(x) for x in proc.communicate(timeout=10)[0].split())
    assert pid == pgid == proc.pid and nofork._session_mode == "setpgroup"


def test_ineligible_calls_keep_plain_popen_semantics(tmp_path):
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'pwd -P; echo "$$ $(ps -o pgid= -p $$)"'], new_session=True,
                              cwd=str(tmp_path), stdout=subprocess.PIPE, text=True)
    out = proc.communicate(timeout=10)[0].split("\n")
    assert out[0] == os.path.realpath(tmp_path)
    pid, pgid = (int(x) for x in out[1].split())
    assert pid == pgid


def test_legacy_env_switch_forces_plain_popen(monkeypatch):
    monkeypatch.setenv("HERMES_SPAWN_LEGACY", "1")
    assert not nofork.eligible(["/bin/true"], {})
    monkeypatch.delenv("HERMES_SPAWN_LEGACY")
    assert nofork.eligible(["/bin/true"], {}) is bool(getattr(subprocess, "_USE_POSIX_SPAWN", False))

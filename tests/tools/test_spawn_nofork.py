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
from tools.file_operations_lint_shell import _parse_check, _parse_bash

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
    import tools.environments.local as local
    proc = nofork.spawn_popen([local._POSIX_SH, "-c", local._CD_THEN_EXEC, "sh",
                               str(tmp_path / "gone"), "/bin/echo", "ran"],
                              new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 126 and "ran" not in out
    assert "hermes: cannot enter working directory:" in out


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
    _parse_bash.cache_clear()
    assert _parse_check("echo ok\n", "bash")[0] is True
    ok, msg = _parse_check("if then fi (\n", "bash")
    assert ok is False and "syntax error" in msg


def test_run_nofork_timeout_kills_child(no_fork_exec):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        nofork.run_nofork(["/bin/sleep", "30"], stdout=subprocess.PIPE, timeout=0.5)
    assert time.monotonic() - started < 10


def test_plain_popen_when_platform_lacks_setsid(monkeypatch):
    real = os.posix_spawn

    def no_setsid(*args, **kwargs):
        if kwargs.get("setsid"):
            raise NotImplementedError("setsid")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawn", no_setsid)
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'echo "$$ $(ps -o pgid= -p $$)"'],
                              new_session=True, stdout=subprocess.PIPE, text=True)
    pid, pgid = (int(x) for x in proc.communicate(timeout=10)[0].split())
    assert pid == pgid == proc.pid


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
    assert nofork.eligible(["/bin/true"], {}) is hasattr(os, "posix_spawn")


def test_popen_function_import_and_execute(monkeypatch, tmp_path):
    import importlib
    import tools.environments.local as local
    real = subprocess.Popen
    calls = []

    def replacement(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", replacement)
    importlib.reload(nofork)
    importlib.reload(local)
    environment = local.LocalEnvironment(cwd=str(tmp_path), timeout=10)
    try:
        result = environment.execute("printf import-ok")
        assert result["returncode"] == 0 and "import-ok" in result["output"]
        assert calls
    finally:
        environment.cleanup()


def test_symlinked_cwd_is_physical(no_fork_exec, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    environment = LocalEnvironment(cwd=str(link), timeout=10)
    try:
        result = environment.execute('printf "%s\\n" "$PWD"')
        assert result["returncode"] == 0 and result["output"].strip() == str(real.resolve())
    finally:
        environment.cleanup()


def test_late_inheritable_fd_is_closed(no_fork_exec, monkeypatch, tmp_path):
    real = os.posix_spawn
    secret = os.open(tmp_path / "secret", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(secret, False)

    def race(*args, **kwargs):
        os.set_inheritable(secret, True)
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawn", race)
    try:
        result = nofork.run_nofork(
            ["/bin/sh", "-c", f'[ -e /dev/fd/{secret} ] && echo leaked || echo closed'],
            capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout.strip() == "closed"
    finally:
        os.close(secret)


def test_explicit_standard_fd_keeps_new_session(monkeypatch, tmp_path):
    output = tmp_path / "session"
    result = nofork.run_nofork(
        ["/bin/sh", "-c", f'echo "$$ $(ps -o sid= -p $$)" > "{output}"'],
        stdout=1, new_session=True)
    pid, sid = map(int, output.read_text().split())
    assert result.returncode == 0 and pid == sid


def test_run_input_and_conflicts(no_fork_exec):
    result = nofork.run_nofork(["/bin/cat"], input="piped", capture_output=True, text=True)
    assert result.stdout == "piped"
    with pytest.raises(ValueError):
        nofork.run_nofork(["/bin/cat"], input="bad", stdin=subprocess.PIPE)

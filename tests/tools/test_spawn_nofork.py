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


@pytest.fixture(scope="session")
def posix_spawn_setsid_supported():
    """Probe the interpreter build, not its OS name or Python version."""
    try:
        pid = os.posix_spawn("/bin/sh", ["/bin/sh", "-c", "exit 0"], os.environ, setsid=True)
    except NotImplementedError:
        return False
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return True


@pytest.fixture(params=["native", "unsupported-setsid"])
def no_fork_exec(monkeypatch, posix_spawn_setsid_supported, request, record_property):
    """Forbid forks except the required session-preserving setsid fallback.

    Run the same real-process semantics with setsid forced unavailable as well.
    Non-session launches must never fork, even on an unsupported interpreter.
    """
    import _posixsubprocess

    supported = posix_spawn_setsid_supported and request.param == "native"
    real_spawn = os.posix_spawn
    real_fork = getattr(subprocess, "_fork_exec", _posixsubprocess.fork_exec)
    attempts = fallbacks = 0
    if not supported:
        record_property("no_fork_assertion_xfail_reason",
                        "posix_spawn setsid unavailable; require isolated Popen fallback instead")

    def spawn(*args, **kwargs):
        nonlocal attempts
        if kwargs.get("setsid") and not supported:
            attempts += 1
            raise NotImplementedError("posix_spawn: setsid unavailable on this platform")
        return real_spawn(*args, **kwargs)

    def checked_fork(*args, **kwargs):
        nonlocal fallbacks
        assert not supported and attempts > fallbacks, (
            "fork_exec called: this spawn forks the whole parent under the GIL")
        pid = real_fork(*args, **kwargs)
        fallbacks += 1
        return pid

    real_execute = subprocess.Popen._execute_child

    def checked_execute(self, *args, **kwargs):
        result = real_execute(self, *args, **kwargs)
        # Popen has now consumed the exec-error pipe. fork_exec returning alone
        # does not mean the child has reached setsid yet.
        assert os.getpgid(self.pid) == os.getsid(self.pid) == self.pid
        return result

    monkeypatch.setattr(subprocess.Popen, "_execute_child", checked_execute)
    monkeypatch.setattr(os, "posix_spawn", spawn)
    monkeypatch.setattr(_posixsubprocess, "fork_exec", checked_fork)
    if hasattr(subprocess, "_fork_exec"):  # 3.12+ binds it at import
        monkeypatch.setattr(subprocess, "_fork_exec", checked_fork)
    yield
    assert attempts == fallbacks, "unsupported setsid did not take the Popen fallback"


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
    result = env.execute(f"printf 'before-timeout\\377'; sleep 300 & echo $! > '{marker}'; sleep 300", timeout=2)
    assert result["returncode"] == 124, result
    assert "before-timeout" in result["output"]
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
            new_session=True, stdout=subprocess.PIPE, text=True)
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
    import _posixsubprocess

    real_fork = getattr(subprocess, "_fork_exec", _posixsubprocess.fork_exec)
    forks = []

    def counted_fork(*args, **kwargs):
        pid = real_fork(*args, **kwargs)
        forks.append(pid)
        return pid

    monkeypatch.setattr(_posixsubprocess, "fork_exec", counted_fork)
    if hasattr(subprocess, "_fork_exec"):
        monkeypatch.setattr(subprocess, "_fork_exec", counted_fork)
    proc = nofork.spawn_popen(["/bin/sh", "-c", 'echo "$$"; sleep 30'],
                              new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert int(proc.stdout.readline()) == proc.pid
        assert forks == [proc.pid], "unsupported setsid must use ordinary Popen"
        assert os.getpgid(proc.pid) == os.getsid(proc.pid) == proc.pid
    finally:
        os.killpg(proc.pid, 9)
        proc.wait(timeout=10)


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


def test_explicit_standard_fd_keeps_new_session(tmp_path):
    output = tmp_path / "session"
    proc = nofork.spawn_popen(
        ["/bin/sh", "-c", f'echo "$$" > "{output}"; sleep 30'],
        stdout=1, new_session=True)
    try:
        assert os.getsid(proc.pid) == proc.pid
    finally:
        os.killpg(proc.pid, 9)
        proc.wait(timeout=10)


@pytest.mark.parametrize("disposition", ["SIG_DFL", "SIG_IGN"])
def test_restore_signals_false_preserves_parent(monkeypatch, disposition):
    import signal

    old = signal.getsignal(signal.SIGPIPE)
    try:
        signal.signal(signal.SIGPIPE, getattr(signal, disposition))
        monkeypatch.setattr(nofork, "_HAVE_CLOSEFROM", False)
        result = nofork.run_nofork(
            ["/bin/sh", "-c", "kill -PIPE $$; printf survived"],
            restore_signals=False, capture_output=True)
        assert result.returncode == (-signal.SIGPIPE if disposition == "SIG_DFL" else 0)
    finally:
        signal.signal(signal.SIGPIPE, old)


def test_exported_functions_preserved_without_overriding_cwd(no_fork_exec, env, tmp_path):
    env.env["BASH_FUNC_greet%%"] = "() { printf function-ok; }"
    env.env["BASH_FUNC_cd%%"] = "() { return 0; }"
    env.env["BASH_FUNC_exec%%"] = "() { return 0; }"
    result = env.execute("greet; printf '\\n'; pwd -P")
    assert result["returncode"] == 0, result
    assert result["output"].strip().splitlines() == [
        "function-ok", os.path.realpath(tmp_path / "work dir")]


def test_wrapped_execute_child_signature(no_fork_exec, monkeypatch):
    real = subprocess.Popen

    class WrappedPopen(real):
        def _execute_child(self, args, executable, *rest, **kw):
            return real._execute_child(self, args, executable, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", WrappedPopen)
    result = nofork.run_nofork(["/bin/cat"], input="wrapped", capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "wrapped"


def test_run_input_and_conflicts(no_fork_exec):
    result = nofork.run_nofork(["/bin/cat"], input="piped", capture_output=True, text=True)
    assert result.stdout == "piped"
    with pytest.raises(ValueError):
        nofork.run_nofork(["/bin/cat"], input="bad", stdin=subprocess.PIPE)

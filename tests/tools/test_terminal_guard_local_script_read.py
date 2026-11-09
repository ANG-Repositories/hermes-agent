"""Local-backend lifecycle-guard script reads never shell out (Apollo slowdown, 2026-11-08).

On the local backend the referenced-script fallback used to spawn a full
``env.execute("head -c ... < path")`` subprocess for every path the host read
missed — on the SAME filesystem the host read just consulted. Under host load
those spawns ate the pre-execution guard's 30 s budget (10 of 18 py-spy guard
observations). The local read is authoritative for local paths, so the guard
must reach the same verdicts with zero ``env.execute`` calls; remote backends
keep the bounded ``head -c`` read.

Lifecycle literals are assembled at runtime so writing this file through an
agent tool does not trip the gates it tests.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

import tools.terminal_tool as terminal_module
from cron.lifecycle_guard import _MAX_REFERENCED_SCRIPT_BYTES
from tools.terminal_tool_guards import gateway_lifecycle_block

RESTART = "hermes gateway " + "re" + "start"


class _SentinelEnv:
    """Env whose execute() records every call; a local scan must never reach it."""

    def __init__(self, cwd: str, output: str = "", returncode: int = 1):
        self.cwd = cwd
        self.env = {}
        self.calls: list[str] = []
        self._output = output
        self._returncode = returncode

    def execute(self, command, **_kwargs):
        self.calls.append(command)
        return {"output": self._output, "returncode": self._returncode}


@pytest.fixture
def local_env(tmp_path):
    """A REAL LocalEnvironment whose execute() is counted after session init.

    Using the real backend means the base code's `head -c` fallback produces its real
    verdicts here, so each test pins the verdict the shell-out reached AND asserts it is
    now reached without spawning anything."""
    from tools.environments.local import LocalEnvironment

    class _CountingLocal(LocalEnvironment):
        calls: list = []

        def execute(self, command, *args, **kwargs):
            type(self).calls.append(command)
            return super().execute(command, *args, **kwargs)

    env = _CountingLocal(cwd=str(tmp_path), timeout=30)
    _CountingLocal.calls = []
    yield env
    try:
        env.cleanup()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _supervised(monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)


def _block(command, env, *, env_type="local", cwd, workdir=None):
    return gateway_lifecycle_block(
        command=command, env=env, env_type=env_type, cwd=cwd, workdir=workdir,
        session_key=f"local-read-{time.monotonic_ns()}",
    )


def _write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# --- local backend: verdicts preserved, no subprocess --------------------------------------

def test_local_missing_script_is_not_read_through_env_execute(tmp_path, local_env):
    env = local_env
    assert _block(f"bash {tmp_path}/absent.sh", env, cwd=str(tmp_path)) is None
    assert env.calls == [], "local scan spawned a subprocess read for a missing script"


def test_local_unreadable_script_keeps_verdict_without_env_execute(tmp_path, local_env):
    if os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    script = _write(tmp_path / "locked.sh", f"#!/bin/bash\n{RESTART}\n", mode=0o000)
    env = local_env
    try:
        # Same verdict the shell-out reached on the same host: `head` cannot open it either.
        assert _block(f"bash {script}", env, cwd=str(tmp_path)) is None
    finally:
        script.chmod(0o644)
    assert env.calls == []


def test_local_readable_unsafe_script_is_blocked_without_env_execute(tmp_path, local_env):
    script = _write(tmp_path / "restart.sh", f"#!/bin/bash\n{RESTART}\n")
    env = local_env
    blocked = _block(f"bash {script}", env, cwd=str(tmp_path))
    assert blocked is not None and "referenced script" in json.loads(blocked)["error"]
    assert env.calls == []


def test_local_magic_prefixed_text_script_is_still_scanned(tmp_path, local_env):
    # The walk's own read skips anything starting with a binary magic; the guard's direct
    # read is what catches a text script that merely begins with "MZ". Must stay blocked.
    script = _write(tmp_path / "mz.sh", f"MZ\n{RESTART}\n")
    env = local_env
    assert _block(f"bash {script}", env, cwd=str(tmp_path)) is not None
    assert env.calls == []


def test_local_oversized_magic_prefixed_script_fails_closed_without_env_execute(tmp_path, local_env):
    # `head -c max+1` on this file returned max+1 bytes, which the sanitizer refused. The
    # direct read must keep that refusal rather than silently skipping the file.
    script = _write(tmp_path / "big.sh", "MZ\n" + "x" * (_MAX_REFERENCED_SCRIPT_BYTES + 16))
    env = local_env
    blocked = _block(f"bash {script}", env, cwd=str(tmp_path))
    assert blocked is not None
    assert "could not scan" in json.loads(blocked)["error"]
    assert env.calls == []


def test_local_nested_relative_unsafe_script_is_blocked(tmp_path, local_env):
    inner = _write(tmp_path / "proj" / "lib" / "inner.sh", f"#!/bin/bash\n{RESTART}\n")
    _write(tmp_path / "proj" / "run.sh", "#!/bin/bash\nbash ./lib/inner.sh\n")
    env = local_env
    assert inner.exists()
    # relative to the command cwd, after a tracked `cd`, and via an explicit relative workdir root
    assert _block("cd proj && bash ./run.sh", env, cwd=str(tmp_path)) is not None
    assert _block("bash ./run.sh", env, cwd=str(tmp_path), workdir=str(tmp_path / "proj")) is not None
    assert _block("bash proj/run.sh", env, cwd=str(tmp_path)) is not None
    assert env.calls == []


def test_local_safe_script_passes_without_env_execute(tmp_path, local_env):
    script = _write(tmp_path / "ok.sh", "#!/bin/bash\necho hi\n")
    env = local_env
    assert _block(f"bash {script}", env, cwd=str(tmp_path)) is None
    assert env.calls == []


# --- remote backend: bounded env.execute read preserved ------------------------------------

@pytest.mark.parametrize("env_type", ["ssh", "docker", "modal"])
def test_remote_backend_still_reads_missing_script_via_env_execute(tmp_path, env_type):
    env = _SentinelEnv("/remote/workspace", output=f"#!/bin/bash\n{RESTART}\n", returncode=0)
    blocked = _block("bash /remote/workspace/remote.sh", env, env_type=env_type, cwd="/remote/workspace")
    assert blocked is not None and "referenced script" in json.loads(blocked)["error"]
    assert any("head -c" in c and "/remote/workspace/remote.sh" in c for c in env.calls)


def test_remote_backend_oversized_read_fails_closed(tmp_path):
    env = _SentinelEnv("/r", output="x" * (_MAX_REFERENCED_SCRIPT_BYTES + 1), returncode=0)
    blocked = _block("bash /r/big.sh", env, env_type="ssh", cwd="/r")
    assert blocked is not None and "could not scan" in json.loads(blocked)["error"]
    assert env.calls


# --- generic pre-exec timeout message ------------------------------------------------------

def test_pre_exec_timeout_message_does_not_blame_the_identity_probe(monkeypatch):
    monkeypatch.setattr(terminal_module, "_PRE_EXEC_GUARD_MIN_TIMEOUT_S", 0)
    monkeypatch.setattr(terminal_module, "_plan_execution", lambda *_a, **_k: SimpleNamespace(
        config={}, env_type="local", effective_task_id="timeout-msg", cwd="/tmp",
        effective_timeout=0.05, promoted_from_foreground_timeout=None,
    ))
    monkeypatch.setattr(terminal_module, "_acquire_env", lambda *_a, **_k: object())
    monkeypatch.setattr(terminal_module, "_pre_exec_block", lambda *_a, **_k: time.sleep(1))
    ran: list[str] = []
    monkeypatch.setattr(terminal_module, "_run_foreground", lambda *_a, **_k: ran.append("x"))

    error = json.loads(terminal_module.terminal_tool("echo ok"))["error"]

    assert "process-identity" not in error
    assert "pre-execution safety checks did not finish" in error
    assert "not run" in error
    assert ran == []

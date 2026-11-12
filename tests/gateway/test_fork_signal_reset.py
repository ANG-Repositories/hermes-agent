"""A signal sent to a gateway fork must not reach the gateway's loop (t_5e351ab7).

Each case runs in a fresh interpreter: an asyncio parent with ``add_signal_handler(SIGTERM)``
forks a sleeping child (no exec) and SIGTERMs the child only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not hasattr(os, "register_at_fork"), reason="POSIX fork only")

REPO = Path(__file__).resolve().parents[2]

_HARNESS = textwrap.dedent(r"""
    import asyncio, json, os, signal, sys, time
    sys.path.insert(0, sys.argv[1])
    install = sys.argv[2] == "1"

    async def main():
        loop = asyncio.get_running_loop()
        fired = []
        if install:
            from gateway.fork_signal_reset import install_fork_signal_reset
            assert install_fork_signal_reset()
        loop.add_signal_handler(signal.SIGTERM, lambda: fired.append(1))
        pid = os.fork()
        if pid == 0:
            time.sleep(5)
            os._exit(7)
        await asyncio.sleep(0.3)
        os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(0.8)
        done, status = os.waitpid(pid, os.WNOHANG)
        if not done:  # child ignored SIGTERM
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            child = "survived"
        elif os.WIFSIGNALED(status):
            child = "signal:%d" % os.WTERMSIG(status)
        else:
            child = "exit:%d" % os.WEXITSTATUS(status)
        print(json.dumps({"parent_fired": bool(fired), "child": child}))

    asyncio.run(main())
""")


def _run(install: bool, env_extra=None) -> dict:
    env = {**os.environ, **(env_extra or {})}
    out = subprocess.run([sys.executable, "-c", _HARNESS, str(REPO), "1" if install else "0"],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_registration_keeps_child_sigterm_out_of_parent_loop():
    res = _run(install=True)
    assert res["parent_fired"] is False, res


def test_child_handles_its_own_sigterm_after_reset():
    res = _run(install=True)
    assert res["child"] == "signal:%d" % 15, res


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="CPython 3.12+ resets the wakeup fd after fork itself; the leak only exists < 3.12")
def test_without_registration_child_sigterm_fires_parent_handler():
    """The mutation: no registration reproduces the 2026-11-11 gateway shutdown."""
    res = _run(install=False)
    assert res["parent_fired"] is True, res


_WAKEUP_HARNESS = textwrap.dedent(r"""
    import asyncio, os, signal, sys
    sys.path.insert(0, sys.argv[1])
    install = sys.argv[2] == "1"

    async def main():
        loop = asyncio.get_running_loop()
        if install:
            from gateway.fork_signal_reset import install_fork_signal_reset
            assert install_fork_signal_reset()
        loop.add_signal_handler(signal.SIGTERM, lambda: None)
        pid = os.fork()
        if pid == 0:
            prev = signal.set_wakeup_fd(-1)  # what the child inherited after the at-fork hooks ran
            os._exit(0 if prev == -1 else 3)
        _, status = os.waitpid(pid, 0)
        print("child_exit=%d" % os.WEXITSTATUS(status))

    asyncio.run(main())
""")


def _child_wakeup_exit(install: bool) -> int:
    out = subprocess.run([sys.executable, "-c", _WAKEUP_HARNESS, str(REPO), "1" if install else "0"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip().splitlines()[-1].split("=")[1])


def test_registration_clears_wakeup_fd_in_child():
    """Holds the ``set_wakeup_fd(-1)`` line alone: the child sees no inherited wakeup fd."""
    assert _child_wakeup_exit(install=True) == 0


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="CPython 3.12+ resets the wakeup fd after fork itself; the leak only exists < 3.12")
def test_without_registration_child_inherits_parent_wakeup_fd():
    assert _child_wakeup_exit(install=False) == 3


def test_start_gateway_installs_reset_before_add_signal_handler():
    """Wiring contract: start_gateway calls install_fork_signal_reset() before its first add_signal_handler."""
    import ast
    tree = ast.parse((REPO / "gateway" / "run.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_gateway")

    def call_lines(name):
        return sorted(c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and (
            (isinstance(c.func, ast.Name) and c.func.id == name)
            or (isinstance(c.func, ast.Attribute) and c.func.attr == name)))

    installs, adds = call_lines("install_fork_signal_reset"), call_lines("add_signal_handler")
    assert installs, "start_gateway no longer calls install_fork_signal_reset()"
    assert adds, "start_gateway no longer calls add_signal_handler"
    assert installs[0] < adds[0], (installs, adds)


def test_kill_switch_skips_registration(monkeypatch):
    from gateway import fork_signal_reset as mod
    monkeypatch.setattr(mod, "_registered", False)
    monkeypatch.setenv(mod.KILL_SWITCH_ENV, "0")
    calls = []
    monkeypatch.setattr(os, "register_at_fork", lambda **kw: calls.append(kw))
    assert mod.install_fork_signal_reset() is False
    assert calls == []

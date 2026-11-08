"""Deterministic regressions from the operator's PR107 adjudication (no credentials)."""
import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from tools import browser_supervisor as s, browser_use_cli as cli
from tools import browser_use_target as t, browser_vault_tool as v


def test_callback_failure_must_not_claim_nothing_written(monkeypatch):
    route = t.BrowserUseRoute({}, 'ws://synthetic', False)
    monkeypatch.setattr(t, 'route_for_task', lambda _: route)
    monkeypatch.setattr(t, 'begin', lambda *a, **kw: object())
    monkeypatch.setattr(t, 'end', lambda _: None)
    monkeypatch.setattr('tools.browser_tool_session.run_fenced', lambda session, fn: fn())
    writes = []
    def callback():
        writes.append('synthetic-write')
        raise RuntimeError('after write')
    result = json.loads(v._fenced_page_op('task', callback))
    assert writes
    assert 'Nothing was written' not in result['error']
    assert result['outcome'] == 'unknown'


def test_probe_cannot_import_cwd_or_inherited_pythonpath(monkeypatch, tmp_path):
    marker = tmp_path / 'executed'
    (tmp_path / 'browser_harness.py').write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setattr(cli, '_find_cli', lambda: [sys.executable, '-m', 'browser_harness.run'])
    route = t.BrowserUseRoute({'PYTHONPATH': str(tmp_path)}, 'ws://synthetic', False)
    with pytest.raises(RuntimeError):
        t.selected_target(route)
    assert not marker.exists()


def test_route_replaced_in_final_probe_cannot_dispatch(monkeypatch):
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    route = t.record_route('task', {'BU_CDP_WS': 'ws://synthetic'})
    monkeypatch.setattr(t, 'selected_target', lambda _: (1, 'login'))
    calls = []
    monkeypatch.setattr(s.SUPERVISOR_REGISTRY, 'get_or_start', lambda **kw: SimpleNamespace(
        evaluate_runtime=lambda *a, **kw: calls.append(a) or {'ok': True}))
    token = t.begin('task', expected_route=route)
    try:
        view = t.supervisor_for_task('task')
        def swap(_):
            t.record_route('task', {'BU_CDP_WS': 'ws://replacement'})
            return (1, 'login')
        monkeypatch.setattr(t, 'selected_target', swap)
        assert not view.evaluate_runtime('synthetic-write')['ok']
        assert not calls
    finally:
        t.end(token)


@pytest.mark.parametrize('change', ['route', 'lease', 'deadline'])
def test_supervisor_dispatch_rechecks_transaction_after_setup(monkeypatch, change):
    import time
    from dataclasses import replace
    from tools.bot_desktop import lease
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    epoch = [7]
    monkeypatch.setattr(lease, 'assert_agent_may_act', lambda profile=None: SimpleNamespace(epoch=epoch[0]))
    route = t.record_route('task', {'BU_CDP_WS': 'ws://synthetic'}, local=True)
    monkeypatch.setattr(t, 'selected_target', lambda _: (1, 'login'))
    token = t.begin('task', expected_route=route)
    tx = t._pin.get()
    if change == 'deadline':
        tx = replace(tx, deadline=time.monotonic() - 1)
    sup = s.CDPSupervisor('synthetic', 'ws://synthetic')
    sup._loop = SimpleNamespace(is_running=lambda: True)
    sup._active, sup._page_session_id = True, 'initial'
    calls = []
    async def cdp(method, params=None, **kw):
        calls.append(method)
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'type': 'page', 'targetId': 'login'}}}
        if method == 'Target.attachToTarget':
            if change == 'route':
                t.record_route('task', {'BU_CDP_WS': 'ws://replacement'})
            elif change == 'lease':
                epoch[0] += 1
            return {'result': {'sessionId': 'pinned'}}
        return {'result': {}}
    sup._cdp = cdp
    monkeypatch.setattr(s, '_schedule', lambda coro, loop, **kw: asyncio.run(coro))
    try:
        result = sup.evaluate_runtime('synthetic-write', target_id='login', dispatch_guard=tx.dispatch)
        assert not result['ok']
        assert result['outcome'] == 'not_dispatched'
        assert 'Runtime.evaluate' not in calls
    finally:
        t.end(token)


@pytest.mark.platforms('posix')
def test_isolated_probe_real_installed_harness_ipc(monkeypatch, tmp_path):
    import socket
    import zipfile
    import tempfile
    from pathlib import Path
    wheel = Path(__file__).resolve().parents[2] / 'deps/browser_harness-0.1.13-py3-none-any.whl'
    site = tmp_path / 'site'
    if wheel.exists():
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
    else:
        from importlib.metadata import distribution, PackageNotFoundError
        try:
            site = Path(distribution('browser-harness').locate_file(''))
        except PackageNotFoundError:
            pytest.skip('browser-harness not installed; optional downloaded wheel unavailable')
    private_runtime = tempfile.TemporaryDirectory(prefix='bu-')
    runtime = Path(private_runtime.name)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(runtime / 'bu.sock'))
    server.listen()
    server.settimeout(5)
    requests = []
    def serve():
        for _ in range(2):
            conn, _ = server.accept()
            with conn:
                request = json.loads(conn.recv(4096))
                requests.append(request['meta'])
                reply = {'pid': 123} if request['meta'] == 'ping' else {'targetId': 'login'}
                conn.sendall(json.dumps(reply).encode() + b'\n')
    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setattr('sysconfig.get_path', lambda _: str(site))
    route = t.BrowserUseRoute({'BU_NAME': 'scoped', 'BH_RUNTIME_DIR': str(runtime),
        'BH_TMP_DIR': str(runtime)}, 'ws://synthetic', False)
    try:
        assert t.selected_target(route) == (123, 'login')
        thread.join(5)
        assert requests == ['ping', 'current_tab']
    finally:
        server.close()
        thread.join(5)

@pytest.mark.parametrize('phase', ['setup', 'dispatch'])
def test_real_loop_timeout_revokes_setup_or_reports_unknown(phase):
    loop = asyncio.new_event_loop()
    started, stopped = threading.Event(), threading.Event()
    writes = []
    def worker():
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()
    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5)
    sup = s.CDPSupervisor('synthetic', 'ws://synthetic')
    sup._loop, sup._active, sup._page_session_id = loop, True, 'initial'
    async def cdp(method, params=None, **kw):
        if method == 'Target.getTargetInfo':
            if phase == 'setup':
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return {'result': {'targetInfo': {'type': 'page', 'targetId': 'login'}}}
        if method == 'Target.attachToTarget':
            return {'result': {'sessionId': 'pinned'}}
        if method == 'Runtime.evaluate':
            writes.append('synthetic-write')
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return {'result': {}}
    sup._cdp = cdp
    try:
        result = sup.evaluate_runtime('synthetic-write', target_id='login', timeout=.05)
        assert not result['ok']
        assert stopped.wait(2), 'timeout returned without quiescing the coroutine'
        assert result['outcome'] == ('not_dispatched' if phase == 'setup' else 'unknown')
        assert bool(writes) == (phase == 'dispatch')
        # Round-trip the loop after return: revoked setup must not dispatch later.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
        assert bool(writes) == (phase == 'dispatch')
    finally:
        async def drain():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), loop).result(2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()

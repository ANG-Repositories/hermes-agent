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

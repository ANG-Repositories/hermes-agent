"""Bounded verdict chains exercise real auxiliary HTTP calls in disposable homes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import goals


@pytest.fixture
def endpoint():
    calls = []
    replies = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            model = body['model']
            calls.append(model)
            value = replies.get(model, 503)
            if callable(value):
                value = value()
            if isinstance(value, int):
                self.send_response(value)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"unavailable"}}')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': 'test', 'model': model, 'choices': [
                {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': value}}
            ]}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure(home, url, **extra):
    home.mkdir(exist_ok=True)
    route = dict(provider='custom', base_url=url, api_key='test-only', timeout=15)
    config = dict(route, model='primary', total_timeout=60,
                  fallback_chain=[dict(route, model=m) for m in ('sol', 'grok', 'kimi')], **extra)
    (home / 'config.yaml').write_text(json.dumps({'auxiliary': {'goal_judge': config}}))


@pytest.mark.parametrize('winner', ['primary', 'sol', 'grok', 'kimi', None])
@pytest.mark.parametrize('verdict', ['done', 'blocked', 'wait', 'continue'])
def test_ordered_chain_and_failure_accounting(tmp_path, monkeypatch, endpoint, winner, verdict):
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if winner:
        replies[winner] = json.dumps(dict(verdict=verdict, reason='evidence', wait_for_seconds=1))
    manager = goals.GoalManager('test-chain')
    manager.set('produce verified artifact')
    result = manager.evaluate_after_turn('substantive evidence')
    order = ['primary', 'sol', 'grok', 'kimi']
    assert calls == (order[:order.index(winner) + 1] if winner else order)
    assert manager.state.consecutive_transport_failures == (0 if winner else 1)
    assert result['verdict'] == (verdict if winner else 'continue')
    assert (manager.state.status == 'done') == (winner is not None and verdict == 'done')


@pytest.mark.parametrize('raw', ['', '{}', '{"done":"false"}', '{"done":{}}', '{"verdict":"bogus"}', 'not json'])
def test_malformed_cannot_complete_and_success_resets(tmp_path, monkeypatch, endpoint, raw):
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    replies['primary'] = raw
    replies['sol'] = '{"verdict":"continue","reason":"more work"}'
    manager = goals.GoalManager('test-malformed')
    manager.set('verify work')
    manager.state.consecutive_transport_failures = 2
    manager.state.consecutive_parse_failures = 2
    manager._save()
    result = manager.evaluate_after_turn('work remains')
    assert calls == ['primary', 'sol']
    assert result['verdict'] == 'continue'
    assert manager.state.consecutive_transport_failures == 0
    assert manager.state.consecutive_parse_failures == 0


def test_profile_chain_isolation(tmp_path, monkeypatch, endpoint):
    from hermes_constants import set_hermes_home_override
    url, calls, replies = endpoint
    replies['primary'] = '{"verdict":"continue","reason":"valid"}'
    for name in ('a', 'b', 'a'):
        home = tmp_path / name
        configure(home, url)
        set_hermes_home_override(home)
        try:
            result = goals.judge_goal('verify', 'evidence')
            assert result[0] == 'continue' and not result[2] and not result[4]
            from agent.auxiliary_client import _client_cache_key
            assert str(home) in str(_client_cache_key('custom', async_mode=False, model='primary'))
        finally:
            set_hermes_home_override(None)
    assert calls == ['primary', 'primary', 'primary']


def test_deadline_and_cancel_reap_request_owner(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli import goal_judge_chain as chain
    from agent.auxiliary_client import AuxiliaryExplicitCancellation, aux_interrupt_protection
    actual_popen = subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        # An uncooperative provider/socket owner: deadline must terminate, not abandon it.
        proc = actual_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    route = dict(provider='custom', model='test', timeout=1)
    assert chain.run_attempt(route, [], 10, time.monotonic() + 0.2)['category'] == 'timeout'
    assert children[-1].poll() is not None
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with aux_interrupt_protection(active=False, cancel_event=cancel):
            with pytest.raises(AuxiliaryExplicitCancellation):
                chain.run_attempt(route, [], 10, time.monotonic() + 30)
    finally:
        timer.cancel()
        timer.join()
    assert children[-1].poll() is not None


def test_total_budget_and_invalid_config(tmp_path, monkeypatch):
    from hermes_cli import goal_judge_chain as chain
    import hermes_cli.config as config
    monkeypatch.setattr(config, 'load_config', lambda: {'auxiliary': {'goal_judge': {
        'provider': 'custom', 'model': 'primary', 'total_timeout': float('inf'),
        'fallback_chain': [{'provider': 'custom', 'model': 'second'}],
    }}})
    with pytest.raises(ValueError):
        chain.chain_config(1)
    calls = []
    def attempt(route, messages, max_tokens, deadline):
        calls.append(route['model'])
        assert deadline <= started + 0.21
        time.sleep(max(0, deadline - time.monotonic()))
        return dict(category='timeout')
    monkeypatch.setattr(chain, 'run_attempt', attempt)
    routes = [dict(provider='custom', model=m, timeout=10) for m in ('primary', 'second')]
    started = time.monotonic()
    result = chain.judge_chain((routes, 0.2), 'system', 'prompt', 10, goals._parse_judge_response)
    assert calls == ['primary']
    assert result[0] == 'continue' and result[4]

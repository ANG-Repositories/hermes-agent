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

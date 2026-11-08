"""Explicit verdict-chain policy; provider routing remains in auxiliary_client.

Each request has a process owner so deadlines and host cancellation can reap the
actual socket owner, rather than abandoning a billing request on a daemon thread.
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_LABEL = re.compile(r'[^a-zA-Z0-9_.:/-]')


def _label(value):
    return _LABEL.sub('_', str(value))[:100]


def _positive(value, name):
    if isinstance(value, bool):
        raise ValueError(name)
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name)
    return value


def chain_config(timeout):
    from hermes_cli.config import load_config
    config = (load_config().get('auxiliary') or {}).get('goal_judge') or {}
    chain = config.get('fallback_chain')
    if chain is None or chain == []:
        return None
    if not isinstance(chain, list) or len(chain) > 7:
        raise ValueError('fallback_chain must contain at most seven routes')
    timeout = _positive(timeout, 'timeout')
    total = _positive(config.get('total_timeout', timeout), 'total_timeout')
    routes = []
    # Do not inherit primary endpoint, credentials or vendor-specific body into other routes.
    primary = {k: v for k, v in config.items() if k not in ('fallback_chain', 'total_timeout')}
    for entry in [primary, *chain]:
        if not isinstance(entry, dict):
            raise ValueError('route must be a mapping')
        route = dict(entry)
        for key in ('provider', 'model'):
            if not isinstance(route.get(key), str) or not route[key].strip() or route[key] == 'auto':
                raise ValueError('each route requires explicit provider and model')
        route['timeout'] = min(_positive(route.get('timeout', timeout), 'timeout'), timeout)
        routes.append(route)
    return routes, total


def _stop(proc):
    if proc.poll() is None:
        from agent.deadline import kill_process_tree
        kill_process_tree(proc.pid)
    proc.wait()


def run_attempt(route, messages, max_tokens, deadline, cancel_check=None):
    from agent.auxiliary_client import raise_if_aux_cancel_requested
    from hermes_constants import get_hermes_home
    from tools.environments.local import served_profile_child_env

    env = served_profile_child_env(target_home=get_hermes_home(), inherit_credentials=True)
    payload = json.dumps(dict(route=route, messages=messages, max_tokens=max_tokens))
    # -m uses THIS source checkout, not the installed runtime's editable root.
    proc = subprocess.Popen(
        [sys.executable, '-m', 'hermes_cli.goal_judge_chain'],
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        pending = payload
        next_state_check = 0.0
        while True:
            raise_if_aux_cancel_requested()
            if cancel_check is not None and time.monotonic() >= next_state_check:
                next_state_check = time.monotonic() + 0.2
                if cancel_check():
                    from agent.auxiliary_client import AuxiliaryExplicitCancellation
                    raise AuxiliaryExplicitCancellation()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(category='timeout')
            try:
                out, _ = proc.communicate(pending, timeout=min(0.05, remaining))
                raise_if_aux_cancel_requested()
                if cancel_check is not None and cancel_check():
                    from agent.auxiliary_client import AuxiliaryExplicitCancellation
                    raise AuxiliaryExplicitCancellation()
                if proc.returncode:
                    return dict(category='worker_error')
                return json.loads(out)
            except subprocess.TimeoutExpired:
                pending = None
    finally:
        _stop(proc)


def judge_chain(policy, system_prompt, prompt, max_tokens, parse, cancel_check=None):
    from agent.auxiliary_client import raise_if_aux_cancel_requested
    routes, total = policy
    deadline = time.monotonic() + total
    messages = [dict(role='system', content=system_prompt), dict(role='user', content=prompt)]
    last_category = 'unavailable'
    for index, route in enumerate(routes):
        raise_if_aux_cancel_requested()
        if time.monotonic() >= deadline:
            last_category = 'total_timeout'
            break
        provider, model = _label(route['provider']), _label(route['model'])
        logger.info('goal judge attempt: rung=%d provider=%s model=%s', index, provider, model)
        outcome = run_attempt(route, messages, max_tokens,
                              min(deadline, time.monotonic() + route['timeout']), cancel_check=cancel_check)
        category = outcome.get('category', 'worker_error')
        if category == 'ok':
            verdict, reason, failed, directive = parse(outcome.get('content', ''))
            if not failed:
                logger.info('goal judge winner: rung=%d provider=%s model=%s served_model=%s',
                            index, provider, model, _label(outcome.get('served_model', 'unknown')))
                return verdict, reason, False, directive, False
            category = 'invalid_output'
        logger.info('goal judge attempt failed: rung=%d provider=%s model=%s category=%s',
                    index, provider, model, category)
        last_category = category
        if category not in {'unavailable', 'timeout', 'invalid_output', 'configuration'}:
            break
    return 'continue', f'goal judge chain failed: {last_category}', last_category == 'invalid_output', None, last_category != 'invalid_output'


def _worker(payload):
    from agent.auxiliary_attempt import single_attempt
    from agent.auxiliary_client import call_llm
    from agent.auxiliary_unavailable import AuxiliaryClientUnavailable
    route = payload['route']
    try:
        with single_attempt('goal_judge', route):
            response = call_llm(task='goal_judge', messages=payload['messages'], temperature=0,
                                max_tokens=payload['max_tokens'], timeout=route['timeout'])
        content = response.choices[0].message.content or ''
        return dict(category='ok', content=content[:16000], served_model=getattr(response, 'model', '') or '')
    except AuxiliaryClientUnavailable:
        return dict(category='unavailable')
    except Exception as exc:
        from agent.auxiliary_client import (
            _is_transient_transport_error, _is_rate_limit_error, _is_auth_error,
            _is_invalid_aux_response_error,
        )
        if isinstance(exc, TimeoutError):
            category = 'timeout'
        elif _is_invalid_aux_response_error(exc):
            category = 'invalid_output'
        elif _is_transient_transport_error(exc) or _is_rate_limit_error(exc) or _is_auth_error(exc):
            category = 'unavailable'
        elif isinstance(exc, (ValueError, KeyError)) or getattr(exc, 'status_code', None) == 404:
            category = 'configuration'
        else:
            category = 'request_error'
        return dict(category=category)


if __name__ == '__main__':
    # stdout is an internal IPC channel; plugin/library prints must not corrupt it.
    import contextlib
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(sys.stderr):
        result = _worker(json.load(sys.stdin))
    print(json.dumps(result))

"""Trusted Browser Use route and selected-target binding (never model stdout)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import subprocess
import threading

from hermes_constants import hermes_home_key


@dataclass(frozen=True)
class BrowserUseRoute:
    env: dict
    cdp_url: str
    local: bool


_lock = threading.Lock()
_routes: dict[tuple[str, str], BrowserUseRoute] = {}
_pin: ContextVar[tuple | None] = ContextVar("browser_use_vault_target", default=None)


def route_for_task(task_id):
    with _lock:
        return _routes.get((hermes_home_key(), task_id or "default"))


def record_route(task_id, env, local=False):
    from tools.browser_tool_cdp import _resolve_cdp_override
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    route = BrowserUseRoute(dict(env), _resolve_cdp_override(endpoint) if endpoint else "", local)
    with _lock:
        _routes[(hermes_home_key(), task_id or "default")] = route
    return route


# This separate, fixed program queries the daemon's private IPC. Agent code/stdout cannot
# forge the result. No credential or secret expression ever enters subprocess arguments.
_PROBE = """import json, os
from browser_harness import _ipc
c, token = _ipc.connect(os.environ.get('BU_NAME', 'default'), timeout=5)
try:
    ping = _ipc.request(c, token, {'meta': 'ping'})
finally:
    c.close()
c, token = _ipc.connect(os.environ.get('BU_NAME', 'default'), timeout=5)
try:
    tab = _ipc.request(c, token, {'meta': 'current_tab'})
finally:
    c.close()
print(json.dumps({'pid': ping.get('pid'), 'target': tab.get('targetId')}))
"""


def selected_target(route):
    from tools.browser_use_cli import _find_cli
    cli = _find_cli()
    if not cli or not route.cdp_url:
        raise RuntimeError("Browser Use target unavailable; run browser_exec on a CDP-routed browser first")
    proc = subprocess.run([cli[0], "-c", _PROBE], env=route.env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=15)
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        if type(data.get("pid")) is not int or not data.get("target"):
            raise ValueError()
        return data["pid"], data["target"]
    except (ValueError, AttributeError):
        raise RuntimeError("Browser Use selected target is closed or unavailable; select a live tab with browser_exec and retry") from None


def begin(task_id):
    route = route_for_task(task_id)
    if route is None:
        raise RuntimeError("No Browser Use route for this task/profile; open the page with browser_exec first")
    generation, target = selected_target(route)
    return _pin.set((task_id, route, generation, target))


def end(token):
    _pin.reset(token)


def supervisor_for_task(task_id):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    pin = _pin.get()
    if pin is None or pin[0] != task_id:
        raise RuntimeError("Browser Use vault target is not pinned; retry the vault operation")
    _, route, generation, target = pin
    if route_for_task(task_id) is not route or selected_target(route) != (generation, target):
        raise RuntimeError("Browser Use route or selected target changed during vault operation; retry")
    supervisor = SUPERVISOR_REGISTRY.get_or_start(
        task_id=f"bu-vault:{hermes_home_key()}:{task_id}", cdp_url=route.cdp_url)
    # Return an immutable target view, not the mutable supervisor page selection.
    class TargetView:
        def evaluate_runtime(self, expression):
            from tools.browser_tool_cdp import _resolve_cdp_override
            if (route_for_task(task_id) is not route
                    or selected_target(route) != (generation, target)
                    or _resolve_cdp_override(route.env.get("BU_CDP_WS") or route.env.get("BU_CDP_URL")) != route.cdp_url):
                return {"ok": False, "error": "Browser Use target/endpoint changed; select a live tab and retry"}
            return supervisor.evaluate_runtime(expression, target_id=target)
    return TargetView()

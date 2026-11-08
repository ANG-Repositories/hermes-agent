"""Trusted Browser Use route and selected-target binding (never model stdout)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from hermes_constants import hermes_home_key


@dataclass(frozen=True)
class BrowserUseRoute:
    env: dict
    cdp_url: str
    local: bool
    ready: bool = True

    def __post_init__(self):
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


_lock = threading.Lock()
_routes: dict[tuple[str, str], BrowserUseRoute] = {}
_pin: ContextVar[VaultTransaction | None] = ContextVar("browser_use_vault_target", default=None)


def route_for_task(task_id):
    with _lock:
        return _routes.get((hermes_home_key(), task_id or "default"))


def record_route(task_id, env, local=False, ready=True):
    from tools.browser_tool_cdp import _resolve_cdp_override
    endpoint = env.get("BU_CDP_WS") or env.get("BU_CDP_URL")
    route = BrowserUseRoute(dict(env), _resolve_cdp_override(endpoint) if endpoint else "", local, ready)
    with _lock:
        _routes[(hermes_home_key(), task_id or "default")] = route
    return route


# This separate, fixed program queries the daemon's private IPC. Agent code/stdout cannot
# forge the result. No credential or secret expression ever enters subprocess arguments.
_PROBE = """import json, os, sys
from pathlib import Path
from importlib.metadata import distributions
# Only explicitly bootstrapped installation roots, never cwd or PYTHONPATH.
for root in json.loads(sys.argv[1]):
    for dist in distributions(path=[root]):
        if dist.metadata.get('Name', '').replace('_', '-').lower() != 'browser-harness':
            continue
        files = {str(f): f for f in dist.files or []}
        entry = files.get('browser_harness/_ipc.py')
        if entry is None or entry.hash is None:
            continue
        import hashlib, base64
        path = Path(dist.locate_file(entry)).resolve()
        if path != Path(root).resolve() / 'browser_harness' / '_ipc.py' or entry.hash.mode != 'sha256':
            continue
        for item in files.values():
            if not str(item).startswith('browser_harness/') or item.hash is None:
                continue
            installed = Path(dist.locate_file(item)).resolve()
            if not installed.is_relative_to(Path(root).resolve()) or item.hash.mode != 'sha256':
                raise RuntimeError('browser-harness invalid installation record')
            digest = base64.urlsafe_b64encode(hashlib.sha256(installed.read_bytes()).digest()).decode().rstrip('=')
            if digest != item.hash.value:
                raise RuntimeError('browser-harness integrity check failed')
        sys.path.insert(0, str(Path(root).resolve()))
        break
    else:
        continue
    break
else:
    raise RuntimeError('Trusted browser-harness installation unavailable')
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
    import sys
    import sysconfig
    if not route.cdp_url or not route.ready:
        raise RuntimeError("Browser Use target unavailable; run browser_exec on a CDP-routed browser first")
    from pm.environments import committed_venv, site_packages

    environment = committed_venv(Path(__file__).resolve().parents[1])
    if environment is not None:
        # PM store Python keeps sys.prefix unchanged. Trust the install's committed
        # dependency record, never inherited PYTHONPATH or a cwd-discovered package.
        roots = {str(site_packages(environment))}
    else:
        roots = {sysconfig.get_path(key) for key in ("purelib", "platlib")}
        bundle = Path(__file__).resolve().parents[2] / "venv"
        roots.update(str(p) for p in (bundle / "Lib" / "site-packages",
            bundle / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages") if p.is_dir())
    env = {k: v for k, v in route.env.items() if not k.startswith("PYTHON")}
    proc = subprocess.run([sys.executable, "-I", "-c", _PROBE, json.dumps(sorted(roots))],
                          env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        if type(data.get("pid")) is not int or not data.get("target"):
            raise ValueError()
        return data["pid"], data["target"]
    except (ValueError, AttributeError):
        raise RuntimeError("Browser Use selected target is closed or unavailable; select a live tab with browser_exec and retry") from None


@dataclass(frozen=True)
class VaultTransaction:
    task_id: str
    profile: str
    route: BrowserUseRoute
    generation: int
    target: str
    lease_epoch: int | None
    deadline: float

    def validate(self):
        from tools.browser_tool_cdp import _resolve_cdp_override
        if (time.monotonic() >= self.deadline
                or selected_target(self.route) != (self.generation, self.target)
                or _resolve_cdp_override(self.route.env.get("BU_CDP_WS") or self.route.env.get("BU_CDP_URL")) != self.route.cdp_url
                or route_for_task(self.task_id) is not self.route):
            raise RuntimeError("Browser Use target/endpoint changed or deadline expired; retry")

    @contextmanager
    def dispatch(self):
        # Route replacement and dispatch authorization have a single linearization
        # point. Never block the supervisor loop waiting for another thread.
        if not _lock.acquire(blocking=False):
            raise RuntimeError("Browser Use route is being replaced; retry")
        try:
            if (_routes.get((self.profile, self.task_id)) is not self.route
                    or time.monotonic() >= self.deadline):
                raise RuntimeError("Browser Use transaction expired or route changed")
            if self.lease_epoch is not None:
                from tools.bot_desktop import lease
                current = lease.assert_agent_may_act(self.profile)
                if current.epoch != self.lease_epoch:
                    raise RuntimeError("Browser Use desktop lease changed; retry")
            yield
        finally:
            _lock.release()


def begin(task_id, *, expected_route):
    route = route_for_task(task_id)
    if route is not expected_route:
        raise RuntimeError("Browser Use route changed after takeover fence selection; retry")
    if route is None:
        raise RuntimeError("No Browser Use route for this task/profile; open the page with browser_exec first")
    deadline = time.monotonic() + 300
    profile = hermes_home_key()
    epoch = None
    if route.local:
        from tools.bot_desktop import lease
        epoch = lease.assert_agent_may_act(profile).epoch
    generation, target = selected_target(route)
    if route_for_task(task_id) is not route:
        raise RuntimeError("Browser Use route changed during target selection; retry")
    return _pin.set(VaultTransaction(task_id, profile, route, generation, target, epoch, deadline))


def end(token):
    _pin.reset(token)


def supervisor_for_task(task_id):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    tx = _pin.get()
    if tx is None or tx.task_id != task_id:
        raise RuntimeError("Browser Use vault target is not pinned; retry the vault operation")
    tx.validate()
    supervisor = SUPERVISOR_REGISTRY.get_or_start(
        task_id=f"bu-vault:{tx.profile}:{task_id}", cdp_url=tx.route.cdp_url)

    class TargetView:
        def evaluate_runtime(self, expression):
            try:
                tx.validate()
            except Exception:
                return {"ok": False, "outcome": "not_dispatched", "error": "Browser Use target/endpoint changed; select a live tab and retry"}
            return supervisor.evaluate_runtime(expression, target_id=tx.target,
                deadline=tx.deadline, dispatch_guard=tx.dispatch)
    return TargetView()

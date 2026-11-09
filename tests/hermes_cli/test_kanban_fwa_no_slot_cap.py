"""allocd owns capacity; legacy per-host slots cannot veto its start door."""
from types import SimpleNamespace

from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp


def test_structural_candidates_ignore_count_and_load_threshold():
    host = SimpleNamespace(enabled=True, state='active', slots=1)
    plan = SimpleNamespace(hosts={'box': host}, detail={'box': {'load1': 999, 'running': 20}}, taken={})
    assert kh.structural_free(plan, 'box') == kwp.UNCOUNTED_TICK_FREE
    host.enabled = False
    assert kh.structural_free(plan, 'box') == 0
    host.enabled = True
    plan.detail['box']['load1'] = None
    assert kh.structural_free(plan, 'box') == 0  # no probe, not a pressure threshold

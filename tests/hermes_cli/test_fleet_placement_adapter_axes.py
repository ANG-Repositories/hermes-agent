"""The adapter lets the pinned reader load a host table that exempts an axis it does not know (t_1cfdbd7b).

2026-11-10: `psi_io_some10` in four Darwin rows' `unmeasurable_axes` made the pinned ``placement.validate()`` reject
the whole fleet table, so ``kanban admit --host`` refused every pool host and the gateway demand publish failed on
every tick. The adapter hides only the names ``admission.DARWIN_UNMEASURABLE`` declares and ``REQUIRED_AXES`` lacks.
"""
from __future__ import annotations

import copy

import pytest

import hermes_cli.fleet_placement as fp
from hermes_cli.fleet_placement import admission, placement

TOLERATED = sorted(set(admission.DARWIN_UNMEASURABLE) - set(placement.REQUIRED_AXES))


def _doc(axes):
    return {"hosts": {"mac": {"unmeasurable_axes": axes}, "linux": {}}}


def _axis_errors(errs):
    return [e for e in errs if "unmeasurable_axes" in e]


def test_there_is_a_name_to_tolerate():
    # If a re-vendor makes REQUIRED_AXES cover every DARWIN_UNMEASURABLE name, the adapter block is dead: delete it.
    assert TOLERATED == ["psi_io_some10"]


def test_pinned_validator_alone_rejects_the_name():
    assert _axis_errors(fp._pinned_validate(_doc(["psi_cpu_some10", "psi_io_some10"])))


def test_adapter_accepts_the_darwin_names_and_changes_nothing_else():
    with_name = placement.validate(_doc(["psi_cpu_some10", "psi_mem_some10", "psi_io_some10", "system_pct"]))
    without = placement.validate(_doc(["psi_cpu_some10", "psi_mem_some10", "system_pct"]))
    assert not _axis_errors(with_name)
    assert with_name == without


@pytest.mark.parametrize("axes", [["psi_cpu_some1O"], ["psi_io_some10", "load1"], "psi_io_some10", ["nonsense"], [7]])
def test_every_other_bad_value_is_still_rejected(axes):
    assert _axis_errors(placement.validate(_doc(axes)))


def test_the_row_is_not_rewritten():
    doc = _doc(["psi_cpu_some10", "psi_io_some10"])
    before = copy.deepcopy(doc)
    placement.validate(doc)
    assert doc == before

"""fork-fan v0.2: the ``/fork-fan`` command (parser, seed, CommandDef, gateway + CLI handlers)."""
import asyncio
import json

import pytest

from hermes_cli.commands import resolve_command
from hermes_cli.fork_fan_command import USAGE, build_fork_fan_seed, parse_fork_fan_args


# ── parser ────────────────────────────────────────────────────────────────


def test_bare_items_default_kind_card():
    opts, items = parse_fork_fan_args("t_aaaa1111 t_bbbb2222")
    assert items == ["t_aaaa1111", "t_bbbb2222"]
    assert opts["kind"] == "card" and opts["board"] is None and opts["model"] is None and opts["where"] == []


@pytest.mark.parametrize("kind", ["card", "pr", "path", "text", "TEXT"])
def test_kind_flag(kind):
    opts, items = parse_fork_fan_args(f"--kind {kind} a b")
    assert opts["kind"] == kind.lower() and items == ["a", "b"]


def test_kind_equals_form_and_board():
    opts, items = parse_fork_fan_args("--kind=pr --board ff-p0 o/r#1 o/r#2")
    assert opts["kind"] == "pr" and opts["board"] == "ff-p0" and items == ["o/r#1", "o/r#2"]


def test_model_and_provider_together():
    opts, items = parse_fork_fan_args("--model claude-opus-5-5 --provider claude-alr x")
    assert (opts["model"], opts["provider"], items) == ("claude-opus-5-5", "claude-alr", ["x"])


@pytest.mark.parametrize("args", ["--model m x", "--provider p x"])
def test_model_without_provider_is_usage(args):
    with pytest.raises(ValueError) as exc:
        parse_fork_fan_args(args)
    assert str(exc.value) == USAGE


@pytest.mark.parametrize("args", [
    "--bogus a", "--kind nope a", "--kind", "--board", "--where nokv", "--kind pr --where status=review",
    "--board= a", 'unbalanced "quote',
])
def test_bad_flags_are_usage_never_dropped(args):
    with pytest.raises(ValueError) as exc:
        parse_fork_fan_args(args)
    assert str(exc.value) == USAGE


def test_where_selector_without_items():
    opts, items = parse_fork_fan_args("--where status=review --where assignee=human:apollo")
    assert items == [] and opts["where"] == ["status=review", "assignee=human:apollo"]


def test_quoted_multiword_items_and_double_dash():
    opts, items = parse_fork_fan_args('--kind text "first thing" second -- --not-a-flag')
    assert items == ["first thing", "second", "--not-a-flag"]


def test_empty_is_no_items():
    assert parse_fork_fan_args("") == ({"kind": "card", "board": None, "model": None, "provider": None,
                                        "where": []}, [])


# ── seed ──────────────────────────────────────────────────────────────────


_HOSTILE = ['$(rm -rf ~)', "`id`", "a; echo pwned", "it's \"quoted\""]


def test_seed_carries_items_verbatim_as_json_and_skill_name():
    opts, items = parse_fork_fan_args("--kind text a b")
    seed = build_fork_fan_seed(opts, items, lambda _i: None)
    assert "fork-fan" in seed and "kind: text" in seed
    assert json.dumps(["a", "b"]) in seed
    assert "override: none" in seed


def test_seed_hostile_items_not_interpolated():
    opts = {"kind": "text", "board": None, "model": None, "provider": None, "where": []}
    seed = build_fork_fan_seed(opts, list(_HOSTILE), lambda _i: None)
    assert json.dumps(_HOSTILE, ensure_ascii=False) in seed
    # Each item appears only inside the JSON array, never as a bare shell fragment of its own line.
    for line in seed.splitlines():
        if "$(rm -rf ~)" in line:
            assert line.startswith("items (4,")


def test_seed_override_selects_summary_mode():
    opts, items = parse_fork_fan_args("--model m1 --provider p1 x")
    seed = build_fork_fan_seed(opts, items, lambda _i: None)
    assert "override: summary mode, no cache-sharing fork" in seed and '"m1"' in seed and '"p1"' in seed


def test_seed_uses_skill_invocation_when_installed():
    seen = {}

    def _skill(instr):
        seen["instr"] = instr
        return f"[SKILL fork-fan]\n{instr}"

    opts, items = parse_fork_fan_args("--board B t_1")
    seed = build_fork_fan_seed(opts, items, _skill)
    assert seed.startswith("[SKILL fork-fan]") and "board: B" in seen["instr"] and '["t_1"]' in seen["instr"]
    assert "delegate_task(" not in seed.split("Follow the fork-fan procedure")[0]


# ── registry ──────────────────────────────────────────────────────────────


def test_commanddef_registered_gateway_visible():
    d = resolve_command("fork-fan")
    assert d is not None and d.name == "fork-fan" and d.args_hint == "<items>"
    assert not d.cli_only and len(d.description) <= 100


# ── gateway handler ───────────────────────────────────────────────────────


class _Event:
    def __init__(self, args):
        self._args = args
        self.text = f"/fork-fan {args}"

    def get_command_args(self):
        return self._args


def _gateway(args, monkeypatch, key=None):
    import agent.skill_commands as sc
    from gateway.run_inbound import GatewayInboundMixin
    monkeypatch.setattr(sc, "resolve_skill_command_key", lambda _name: key)
    ev = _Event(args)
    handled, reply = asyncio.run(GatewayInboundMixin._hm_cmd_fork_fan(None, ev, None, "k"))
    return handled, reply, ev.text


def test_gateway_empty_returns_usage_no_turn(monkeypatch):
    handled, reply, text = _gateway("", monkeypatch)
    assert handled and reply == USAGE and text == "/fork-fan "


def test_gateway_bad_flag_returns_usage(monkeypatch):
    handled, reply, _ = _gateway("--model m a", monkeypatch)
    assert handled and reply == USAGE


def test_gateway_rewrites_and_falls_through(monkeypatch):
    handled, reply, text = _gateway("--kind text a b", monkeypatch)
    assert (handled, reply) == (False, None)
    assert "kind: text" in text and '["a", "b"]' in text and "fork-fan" in text


def test_gateway_dispatch_resolves_dashed_canonical():
    from gateway.run import GatewayRunner
    assert "fork-fan" in GatewayRunner._HM_CANONICAL_COMMANDS
    assert callable(getattr(GatewayRunner, "_hm_cmd_fork_fan", None))


# ── CLI handler ───────────────────────────────────────────────────────────


def test_cli_handler_resolves_and_queues_seed(monkeypatch):
    import agent.skill_commands as sc
    from cli import HermesCLI
    assert HermesCLI._slash_handler("fork-fan") == ("_handle_fork_fan_command", True)
    monkeypatch.setattr(sc, "resolve_skill_command_key", lambda _name: None)
    obj = type("O", (), {})()
    assert HermesCLI._handle_fork_fan_command(obj, "/fork-fan --kind text a b") is True
    assert '["a", "b"]' in obj._pending_agent_seed


def test_cli_handler_empty_prints_usage_queues_nothing():
    from cli import HermesCLI
    obj = type("O", (), {})()
    assert HermesCLI._handle_fork_fan_command(obj, "/fork-fan") is True
    assert not hasattr(obj, "_pending_agent_seed")

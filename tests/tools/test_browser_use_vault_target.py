"""Browser Use vault must follow its selected target, never origin-search another tab."""
import json
from types import SimpleNamespace

import pytest

from tools import browser_use_cli as bu
from tools import browser_vault_tool as vault


def test_browser_use_origin_probe_does_not_search_other_tabs(monkeypatch):
    calls = []
    supervisor = SimpleNamespace(focus_page=lambda *a, **kw: calls.append(a) or {"ok": True})
    monkeypatch.setattr(bu, "is_browser_use_cli_mode", lambda: True)
    monkeypatch.setattr(vault, "_ensure_supervisor", lambda tid: supervisor)
    assert vault._focus_bound_origin("task", "https://appleid.apple.com", "login") is None
    assert calls == []

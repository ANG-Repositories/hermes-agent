"""The navigation driver's current page, not an origin search, owns vault writes.

Only synthetic address data is used. The CDP supervisor and JavaScript execute
against real disposable Chromium pages; the CLI boundary uses that driver's page.
"""
import json
from types import SimpleNamespace

import pytest

from tools import browser_tool as bt, browser_vault_tool as vault
from tools.browser_supervisor import CDPSupervisor, SUPERVISOR_REGISTRY


@pytest.fixture(params=["fixture-task", "fixture-task::local"])
def pages(monkeypatch, request):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--remote-debugging-port=0", "--enable-automation"])
        context = browser.new_context()
        context.route("https://*.example.test/**", lambda route: route.fulfill(
            body='<input autocomplete="address-line1" name="address">', content_type="text/html"))
        wrong = context.new_page()
        wrong.goto("https://site.example.test/other")
        current = context.new_page()
        current.goto("https://site.example.test/current")
        # Connect the real supervisor to the same browser over its browser WS.
        endpoint = browser.new_browser_cdp_session().send("Browser.getBrowserCommandLine")
        # Playwright's pipe launch does not expose its chosen port. Obtain the
        # endpoint through DevToolsActivePort in its isolated user-data directory.
        from pathlib import Path
        profile = next(a.split("=", 1)[1] for a in endpoint["arguments"] if a.startswith("--user-data-dir="))
        port, ws_path = (Path(profile) / "DevToolsActivePort").read_text().splitlines()[:2]
        sup = CDPSupervisor("fixture-task", f"ws://127.0.0.1:{port}{ws_path}")
        sup.start()
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: False)
        key = request.param
        session = {"owner_task_id": "fixture-task", "session_key": key}
        monkeypatch.setitem(bt._active_sessions, key, session)
        monkeypatch.setitem(bt._last_active_session_key, "fixture-task", key)
        monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda target: sup if target == key else None)
        driver = {"page": current}
        calls = []
        def command(key, name, args, **kwargs):
            calls.append((key, name))
            assert key == request.param
            assert name == "eval", "fixture must not create a browser or resolve credentials"
            return {"success": True, "data": {"result": driver["page"].evaluate(args[0])}}
        monkeypatch.setattr("tools.browser_tool_session._run_browser_command", command)
        meta = SimpleNamespace(id="fixture-address", kind="address", fill_supported=True,
                               origin="https://site.example.test", allowed_origins=(), label="Fixture")
        backend = SimpleNamespace(name="fixture", needs_unlock=False, get_meta=lambda _: meta,
                                  resolve_secret=lambda _: {"address_line1": "NONSECRET FIXTURE STREET"})
        monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
        try:
            yield current, wrong, driver, backend, meta, sup, calls
        finally:
            sup.stop()
            browser.close()


def fill():
    return json.loads(vault.browser_vault_fill("fixture-address", task_id="fixture-task"))


def test_unsupported_backend_cannot_reuse_builtin_session(pages, monkeypatch):
    current, wrong, _, _, _, _, calls = pages
    for mode in ("_is_camofox_mode", "_is_browser_use_cli_mode"):
        with monkeypatch.context() as patch:
            patch.setattr(bt, mode, lambda: True)
            out = fill()
            assert out["error_type"] == "page_binding_required", out
    assert calls == []
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""


def test_navigation_current_page_wins_over_first_same_origin_tab(pages):
    current, wrong, *_ = pages
    out = fill()
    assert out["success"], out
    assert current.locator("input").input_value() == "NONSECRET FIXTURE STREET"
    assert wrong.locator("input").input_value() == ""


def test_bound_origin_mismatch_never_searches_another_tab(pages):
    current, wrong, _, _, meta, *_ = pages
    current.goto("https://accounts.example.test/login")
    out = fill()
    assert out["error_type"] == "origin_mismatch", out
    assert wrong.locator("input").input_value() == ""
    assert current.locator("input").input_value() == ""


@pytest.mark.parametrize("race", ["same_origin_navigation", "cross_origin_navigation", "tab_switch", "session_replaced", "camofox_switch", "browser_use_switch"])
def test_page_change_during_resolution_fails_closed(pages, race, monkeypatch):
    current, wrong, driver, backend, *_ = pages
    def resolve(_):
        if race == "same_origin_navigation":
            current.goto("https://site.example.test/replaced")
        elif race == "cross_origin_navigation":
            current.goto("https://accounts.example.test/replaced")
        elif race == "tab_switch":
            driver["page"] = wrong
        elif race in {"camofox_switch", "browser_use_switch"}:
            mode = "_is_camofox_mode" if race == "camofox_switch" else "_is_browser_use_cli_mode"
            monkeypatch.setattr(bt, mode, lambda: True)
        else:
            key = bt._last_session_key("fixture-task")
            monkeypatch.setitem(bt._active_sessions, key, dict(bt._active_sessions[key]))
        return {"address_line1": "NONSECRET FIXTURE STREET"}
    backend.resolve_secret = resolve
    out = fill()
    assert not out["success"], out
    assert current.locator("input").input_value() == ""
    assert wrong.locator("input").input_value() == ""


def test_foreign_task_cannot_use_supervisor_or_current_page(pages):
    current, wrong, _, _, _, _, calls = pages
    out = json.loads(vault.browser_vault_fill("fixture-address", task_id="foreign-task"))
    assert not out["success"], out
    assert calls == []
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""


@pytest.mark.parametrize("race", ["navigation", "tab_switch", "session_swap", "camofox_switch", "browser_use_switch"])
def test_navigation_after_target_selection_is_atomically_refused(pages, monkeypatch, race):
    current, wrong, driver, _, _, sup, _ = pages
    focus = sup.focus_page

    def raced_focus(*args, **kwargs):
        result = focus(*args, **kwargs)
        if race == "navigation":
            current.goto("https://site.example.test/new-document")
        elif race == "tab_switch":
            driver["page"] = wrong
        elif race in {"camofox_switch", "browser_use_switch"}:
            mode = "_is_camofox_mode" if race == "camofox_switch" else "_is_browser_use_cli_mode"
            monkeypatch.setattr(bt, mode, lambda: True)
        else:
            key = bt._last_session_key("fixture-task")
            monkeypatch.setitem(bt._active_sessions, key, dict(bt._active_sessions[key]))
        return result

    monkeypatch.setattr(sup, "focus_page", raced_focus)
    out = fill()
    assert not out["success"], out
    assert current.locator("input").input_value() == wrong.locator("input").input_value() == ""

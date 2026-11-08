"""Synthetic HTTP providers, real boomerang entry and conversation fallback engine.

Only provider credential lookup and tool catalog discovery are substituted. No
production provider, platform delivery, card or gateway process is involved.
"""
from __future__ import annotations

import asyncio
import json
import signal
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as wake
from run_agent import AIAgent
from tools import delegate_tool as dt


@contextmanager
def endpoints(sentinel):
    trace = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "model" not in body:
                self.send_error(404)
                return
            lane = self.path.split("/")[1]
            trace.append({"ordinal": len(trace) + 1, "lane": lane, "model": body["model"]})
            if lane != "cross-vendor":
                self.send_response(429)
                payload = {"error": {"message": "insufficient_quota: synthetic provider exhausted", "type": "insufficient_quota", "code": "insufficient_quota"}}
            else:
                self.send_response(200)
                payload = {"id": "synthetic-response", "object": "chat.completion", "created": 1,
                           "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": sentinel}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
            if lane == "cross-vendor" and body.get("stream"):
                chunk = {"id": "synthetic-response", "object": "chat.completion.chunk", "created": 1,
                         "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": sentinel}, "finish_reason": "stop"}]}
                encoded = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                encoded = json.dumps(payload).encode()
                content_type = "application/json"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", trace
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("scenario", ["summary", "downgrade", "busy", "cold", "detached"])
def test_real_boomerang_fails_over_to_wire_sentinel(monkeypatch, tmp_path, scenario):
    from openai import OpenAI
    import model_tools
    import agent.auxiliary_client as auxiliary

    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [])
    monkeypatch.setattr(model_tools, "check_toolset_requirements", lambda **kw: {})
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "wrong-worker-default", "fallback_providers": [], "max_iterations": 8})
    monkeypatch.setenv("SYNTHETIC_FALLBACK_KEY", "synthetic-key")
    def expired(*_):
        raise TimeoutError("boomerang HTTP case exceeded 30 seconds")
    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.alarm(30)
    sentinel = "BOOMERANG-WIRE-" + uuid.uuid4().hex
    with endpoints(sentinel) as (base, trace):
        chain = [{"provider": "openrouter", "model": "second-model", "base_url": base + "/second/v1"},
                 {"provider": "openai", "model": "cross-model", "base_url": base + "/cross-vendor/v1"}]

        def resolve(provider, model=None, **kw):
            return OpenAI(api_key="synthetic-key", base_url=kw["explicit_base_url"], max_retries=0), model

        monkeypatch.setattr(auxiliary, "resolve_provider_client", resolve)
        parent = AIAgent(model="parent-pin", provider="openrouter", api_mode="chat_completions",
                         api_key="synthetic-key", base_url=base + "/primary/v1", fallback_model=chain,
                         enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True)
        parent._session_messages = [{"role": "assistant", "content": "parent context"}]
        home = SimpleNamespace(session_id=parent.session_id, session_key="agent:main:discord:group:synthetic", origin=None)
        store = SimpleNamespace(load_transcript=lambda sid: parent._session_messages)
        runner = SimpleNamespace(session_store=store,
                                 _resolve_session_agent_runtime=lambda **kw: (parent.model, {"provider": parent.provider, "api_mode": parent.api_mode, "api_key": parent.api_key, "base_url": parent.base_url}),
                                 _refresh_fallback_model=lambda: chain)
        monkeypatch.setattr("gateway.kanban_owner_wake.resolve_owner", lambda *a: home)
        monkeypatch.setattr(wake, "_live_agent", lambda *a: (None, False) if scenario == "cold" else (parent, scenario == "busy"))
        try:
            selected, mode = asyncio.run(wake.resolve_parent(runner, {"home_sid": parent.session_id, "mode": "fork" if scenario == "downgrade" else "summary"}, wait_s=0))
            if scenario == "detached":
                from gateway.session_context import set_session_vars, restore_session_vars
                from tools.delegate_detached import build_parent_and_runner
                captured_children = []
                original_builder = dt._build_child_agent

                def capture(**kw):
                    child = original_builder(**kw)
                    captured_children.append(child)
                    return child

                monkeypatch.setattr(dt, "_build_child_agent", capture)
                status, final = wake.run_child(selected, "Return the provider's response verbatim.", mode)
                tokens = set_session_vars(platform="discord", source="gateway", chat_id="synthetic", session_key=home.session_key, session_id=parent.session_id)
                try:
                    record, _ = dt._build_durable_background_spec(
                        task_list=[{"goal": "Return the provider's response verbatim.", "inherit_mode": "summary"}],
                        shared_context=None, top_role="leaf", inherit_context=True,
                        cfg={"model": "wrong-worker-default", "fallback_providers": []},
                        creds={"model": None, "provider": None, "base_url": None, "api_mode": None},
                        parent_agent=parent, children=captured_children,
                        session_key=home.session_key, parent_session_id=parent.session_id,
                        origin_ui_session_id="", max_iterations=8,
                    )
                finally:
                    restore_session_vars(tokens)
                serialized = json.dumps(record)
                assert "synthetic-key" not in serialized
                restored = json.loads(serialized)
                assert restored["source"]["tasks"][0]["parent_route"]["source"] == "parent"
                # Simulate new defaults and a fresh credential resolution, not the original parent/client.
                def runtime(*a, **kw):
                    return {"provider": "openrouter", "api_mode": "chat_completions", "api_key": "synthetic-key", "base_url": base + "/primary/v1"}
                monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", runtime)
                recovered, _, rebuilt = build_parent_and_runner(restored, None, {"model": "new-worker-default"}, resolve_default=runtime, resolve_for_provider=runtime)
                trace.clear()
                try:
                    data = recovered()
                    item = data["results"][0]
                    status, final = item["status"], item["summary"]
                finally:
                    rebuilt.close()
            else:
                status, final = wake.run_child(selected, "Return the provider's response verbatim.", mode)
            (tmp_path / "wire-trace.json").write_text(json.dumps({"scenario": scenario, "trace": trace, "status": status, "final": final}, indent=2))
            print(json.dumps({"scenario": scenario, "trace": trace, "status": status, "sentinel": sentinel, "final": final}))
            assert status == "completed", final
            assert sentinel in final
            assert [row["lane"] for row in trace] == ["primary", "second", "cross-vendor"]
            assert [row["model"] for row in trace] == ["parent-pin", "second-model", "cross-model"]
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
            parent.close()

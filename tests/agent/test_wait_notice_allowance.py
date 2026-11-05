"""Replay the solo/shared wait notices from the 2026-11-04 allowance incident."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent import fallback_wall_budget as wb


@pytest.fixture
def relay(monkeypatch):
    payload = {"stage": "upstream_ttfb", "seat": "sub-vps-5", "since_ms": 69000,
               "model": "claude-fable-5-1",
               "allowance": {"seats_total": 26, "seats_exhausted": 21,
                             "seats_eligible": 3, "seat_inflight": 27,
                             "earliest_reset": "2026-11-05T06:00:00Z", "seat_share_5m": 0.59}}
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(wb, "_knobs", lambda: (600, 120))
    monkeypatch.setattr(wb, "_dedupe_on", lambda: True)
    monkeypatch.setattr(wb, "post_logs", lambda text: None)
    wb.clear_registry()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield payload, paths, base
    finally:
        wb.clear_registry()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("case", ["allowance", "absent", "zero", "bad_reset", "upload",
                                  "malformed", "unknown_count", "no_exhaustion"])
def test_wait_notice_replays(relay, shared, case):
    payload, paths, base = relay
    if case == "absent":
        payload.pop("allowance")
    elif case == "zero":
        payload["allowance"].update(seats_exhausted=26, seats_eligible=0)
    elif case == "bad_reset":
        payload["allowance"]["earliest_reset"] = "not-a-date"
    elif case == "upload":
        payload["stage"] = "relay_send"
    elif case == "malformed":
        payload["allowance"] = []
    elif case == "unknown_count":
        payload["allowance"]["seats_eligible"] = None
    elif case == "no_exhaustion":
        payload["allowance"]["seats_exhausted"] = 0
    agents = [SimpleNamespace(provider="claude-alr", model="claude-fable-5-1", base_url=base,
                              session_id=f"session-{i}", _fallback_chain=[{"provider": "cpa"}])
              for i in range(4 if shared else 1)]
    for i, agent in enumerate(agents):
        wb.arm(agent, now=-23 if shared and i == 0 else 0)
        wb.note_quiet(agent, 200, now=0)
    text = wb.notice_due(agents[-1], now=120)
    fallback = case in {"absent", "malformed", "unknown_count", "no_exhaustion"}
    eligible = 0 if case == "zero" else 3
    reset = "" if case == "bad_reset" else "22:00 PT"
    if shared:
        prefix = "⏳ 4 sessions waiting on claude-alr/claude-fable-5-1 (oldest 143s)"
        if fallback:
            stage = ""
        elif case == "upload":
            stage = ": request still uploading to sub-vps-5 (our uplink) for 69s"
        else:
            stage = f": claude-fable-5-1 weekly cap, {eligible} of 26 seats left"
            stage += f", resets {reset}" if reset else ""
        expected = f"{prefix}{stage}; this turn 120s, cut at 600s"
    else:
        if fallback:
            stage = "sub-vps-5 connected, provider has not sent a first token for 69s"
        elif case == "upload":
            stage = "request still uploading to sub-vps-5 (our uplink) for 69s"
        else:
            stage = f"claude-fable-5-1 weekly cap: {eligible} of 26 seats left, 27 turns on sub-vps-5"
            stage += f" · resets {reset}" if reset else ""
        expected = f"⏳ {stage}: 120s elapsed; cut at 600s"
    print(text)
    assert text == expected
    assert paths == [f"/inflight?call={agents[-1].session_id}"]
    assert wb.notice_due(agents[-1], now=121) is None
    if shared:
        for agent in agents:
            wb.note_quiet(agent, 300, now=120)
        assert wb.notice_due(agents[-1], now=240) is None
        assert len(paths) == 1

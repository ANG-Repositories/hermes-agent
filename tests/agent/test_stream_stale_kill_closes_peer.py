"""A stale-killed stream must close the connection as the SERVER sees it.

t_3c67bf68: the claude-pool relay frees an abandoned route's handler only
when it observes its client's socket close (in-flight disconnect watch). On
2026-11-03 the agent stale-killed 75 silent claude-alr streams at 360 s and
retried; the relay's release of the orphan depends on the kill reaching the
wire. This pins that contract on the Anthropic Messages wire (the claude-alr
transport), for a silence both before and after the response headers: the
peer must see EOF within 1 s of the stale threshold, while the attempt is
being abandoned, not at the read timeout.
"""
import json
import socket
import threading
import time

import pytest

import run_agent

STALE_S = 2.0
READ_TIMEOUT_S = 25.0
EOF_BUDGET_S = 1.0


class _SilentMessagesServer:
    """POST /v1/messages #1: silent (optionally after 200 + SSE headers) until
    the client closes; records when EOF arrives. Other paths: 404."""

    def __init__(self, send_headers: bool):
        self.send_headers = send_headers
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.posts = 0
        self.held_at = None
        self.eof_at = None
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _read_request(c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(65536)
            if not d:
                return None
            buf += d
        head, rest = buf.split(b"\r\n\r\n", 1)
        n = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                n = int(line.split(b":", 1)[1])
        while len(rest) < n:
            d = c.recv(65536)
            if not d:
                break
            rest += d
        return head.split(b"\r\n", 1)[0]

    def _serve(self, c):
        c.settimeout(READ_TIMEOUT_S + 5)
        try:
            while True:
                line = self._read_request(c)
                if line is None:
                    return
                if not (line.startswith(b"POST") and b"/v1/messages" in line):
                    c.sendall(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n")
                    continue
                self.posts += 1
                if self.posts > 1:
                    body = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                                  "message": "test"}}).encode()
                    c.sendall(b"HTTP/1.1 529 Overloaded\r\ncontent-type: application/json\r\n"
                              b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                    continue
                if self.send_headers:
                    c.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                              b"transfer-encoding: chunked\r\n\r\n")
                self.held_at = time.time()
                try:
                    c.recv(1)
                except OSError:
                    pass
                self.eof_at = time.time()
                return
        except OSError:
            return
        finally:
            c.close()

    def close(self):
        self._stop.set()
        self.sock.close()


@pytest.mark.parametrize("send_headers", [False, True], ids=["before_headers", "after_headers"])
def test_stale_kill_closes_the_anthropic_stream_socket(monkeypatch, send_headers):
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", str(int(STALE_S)))
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    monkeypatch.setenv("HERMES_STREAM_READ_TIMEOUT", str(int(READ_TIMEOUT_S)))
    srv = _SilentMessagesServer(send_headers)
    try:
        agent = run_agent.AIAgent(
            api_key="test-key", base_url=f"http://127.0.0.1:{srv.port}", model="m",
            provider="anthropic", api_mode="anthropic_messages", platform="cli",
            quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[],
            max_iterations=1,
        )
        agent._interrupt_requested = False
        with pytest.raises(Exception):
            agent._interruptible_streaming_api_call(
                {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})
        end = time.time() + READ_TIMEOUT_S
        while srv.eof_at is None and time.time() < end:
            time.sleep(0.02)
        assert srv.posts == 1
        assert srv.held_at is not None
        assert srv.eof_at is not None, "the server never saw the killed attempt's socket close"
        waited = srv.eof_at - srv.held_at
        assert waited < STALE_S + EOF_BUDGET_S, (
            f"the server saw EOF {waited:.2f}s after the request was held; the stale "
            f"kill at {STALE_S:.0f}s did not close the connection")
    finally:
        srv.close()

"""M4: the HTTP surface.

The tests that matter here are the authorization ones. This port accepts a POST
that injects text into a running agent's context, which makes it an instruction
channel, not a status page — and an unauthenticated instruction channel on
localhost is reachable by anything else running on the machine, including a
browser tab on a hostile page.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from optimus.surface.control import Control, SteerKind
from optimus.surface.events import Bus, EventKind
from optimus.surface.server import SurfaceServer


def _get(server, path, *, token=None, headers=None):
    request = urllib.request.Request(f"{server.url}{path}")
    tok = server.token if token is None else token
    if tok:
        request.add_header("Authorization", f"Bearer {tok}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


def _post(server, path, body, *, token=None):
    request = urllib.request.Request(
        f"{server.url}{path}", data=json.dumps(body).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    tok = server.token if token is None else token
    if tok:
        request.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


@pytest.fixture
def served():
    bus = Bus(run_id="r1")
    control = Control(run_id="r1")
    server = SurfaceServer(bus, control).start()
    try:
        yield server, bus, control
    finally:
        server.stop()


# --------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------

class TestAuthorization:
    def test_a_token_is_generated_rather_than_optional(self):
        """There is no code path that serves an unauthenticated steer channel.
        A caller who wants no auth has to pass a token and share it."""
        bus = Bus()
        server = SurfaceServer(bus, Control())
        assert server.token and len(server.token) >= 20
        server.stop()

    def test_steering_without_a_token_is_refused(self, served):
        server, _bus, control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(server, "/steer", {"text": "do something else"}, token="")
        assert caught.value.code == 401
        assert control.pending == 0

    def test_a_wrong_token_is_refused(self, served):
        server, _bus, _control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(server, "/run", token="not-the-token")
        assert caught.value.code == 401

    def test_a_token_prefix_is_not_enough(self, served):
        """Guards the early-return comparison that leaks its own prefix."""
        server, _bus, _control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(server, "/run", token=server.token[:-1])
        assert caught.value.code == 401

    def test_healthz_is_the_only_unauthenticated_route(self, served):
        server, _bus, _control = served
        status, body = _get(server, "/healthz", token="")
        assert status == 200 and body == {"ok": True}

    def test_a_token_in_the_query_string_works_for_the_stream(self, served):
        """EventSource in a browser cannot set headers, so the stream has to
        accept the token another way."""
        server, _bus, _control = served
        status, _body = _get(server, f"/run?token={server.token}", token="")
        assert status == 200

    def test_binding_off_loopback_is_refused_by_default(self):
        with pytest.raises(ValueError, match="refusing to bind"):
            SurfaceServer(Bus(), Control(), host="0.0.0.0")

    def test_binding_off_loopback_is_possible_deliberately(self):
        server = SurfaceServer(
            Bus(), Control(), host="0.0.0.0", allow_non_loopback=True
        )
        assert server.port
        server.stop()


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------

class TestControlRoutes:
    def test_a_steer_is_accepted_and_queued(self, served):
        server, _bus, control = served
        status, body = _post(
            server, "/steer", {"kind": "guidance", "text": "use the makefile"}
        )
        assert status == 202
        assert body["accepted"]["source"] == "http"
        assert control.pending == 1
        assert control.peek()[0].kind is SteerKind.GUIDANCE

    def test_every_steer_kind_is_reachable_over_http(self, served):
        server, _bus, control = served
        for kind in ("note", "guidance", "interrupt"):
            status, _ = _post(server, "/steer", {"kind": kind, "text": f"a {kind}"})
            assert status == 202
        assert control.pending == 3
        # And they come back out in priority order, not arrival order.
        assert [s.kind for s in control.drain()] == [
            SteerKind.INTERRUPT, SteerKind.GUIDANCE, SteerKind.NOTE
        ]

    def test_an_unknown_kind_is_rejected_with_the_known_ones(self, served):
        server, _bus, control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(server, "/steer", {"kind": "obliterate", "text": "hi"})
        assert caught.value.code == 400
        assert control.pending == 0

    def test_an_empty_steer_is_rejected(self, served):
        server, _bus, control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(server, "/steer", {"kind": "note", "text": "   "})
        assert caught.value.code == 400
        assert control.pending == 0

    def test_cancel_trips_the_stop_bit(self, served):
        server, _bus, control = served
        status, body = _post(server, "/cancel", {"reason": "changed my mind"})
        assert status == 202 and body["cancelled"] is True
        assert control.cancelled
        assert control.cancel_reason == "changed my mind"

    def test_a_run_with_no_control_plane_refuses_to_be_steered(self):
        bus = Bus(run_id="r")
        server = SurfaceServer(bus, None).start()
        try:
            with pytest.raises(urllib.error.HTTPError) as caught:
                _post(server, "/steer", {"text": "hello"})
            assert caught.value.code == 409
        finally:
            server.stop()

    def test_the_snapshot_reports_what_is_queued(self, served):
        server, _bus, control = served
        control.guide("one")
        control.signal(0.4, source="model", reason="unfamiliar build system")
        _status, body = _get(server, "/run")
        assert body["run_id"] == "r1"
        assert body["pending_steers"] == 1
        assert body["confidence"] == {
            "value": 0.4, "source": "model",
            "reason": "unfamiliar build system",
        }

    def test_an_unknown_route_is_a_404(self, served):
        server, _bus, _control = served
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(server, "/nope")
        assert caught.value.code == 404


# --------------------------------------------------------------------------
# the stream
# --------------------------------------------------------------------------

class TestEventStream:
    def _read_frames(self, server, path, count, *, timeout=10):
        """Read `count` SSE data frames off a live stream."""
        frames: list[dict] = []
        request = urllib.request.Request(f"{server.url}{path}")
        request.add_header("Authorization", f"Bearer {server.token}")
        response = urllib.request.urlopen(request, timeout=timeout)

        def reader():
            for raw in response:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if len(frames) >= count:
                        return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        return frames, thread, response

    def test_the_stream_emits_ag_ui_events_by_default(self, served):
        server, bus, _control = served
        frames, thread, response = self._read_frames(server, "/events", 2)
        # Give the subscription a moment to attach before publishing.
        for _ in range(100):
            if bus.subscribers:
                break
            threading.Event().wait(0.02)
        bus.publish(EventKind.RUN_STARTED, model="qwen35-9b")
        bus.publish(EventKind.TURN_STARTED, turn=1)
        thread.join(timeout=10)
        response.close()

        assert len(frames) >= 2
        assert frames[0]["type"] == "RUN_STARTED"
        assert frames[1]["type"] == "STEP_STARTED"

    def test_the_raw_bus_is_available_too(self, served):
        server, bus, _control = served
        frames, thread, response = self._read_frames(
            server, "/events?format=optimus", 1
        )
        for _ in range(100):
            if bus.subscribers:
                break
            threading.Event().wait(0.02)
        bus.publish(EventKind.TOOL_CALL, turn=2, name="bash", brief="ls")
        thread.join(timeout=10)
        response.close()

        assert frames[0]["kind"] == "tool.call"
        assert frames[0]["payload"]["name"] == "bash"
        assert frames[0]["turn"] == 2

    def test_the_subscription_is_released_when_the_client_goes_away(self, served):
        server, bus, _control = served
        _frames, thread, response = self._read_frames(server, "/events", 1)
        for _ in range(100):
            if bus.subscribers:
                break
            threading.Event().wait(0.02)
        assert bus.subscribers == 1
        bus.publish(EventKind.RUN_STARTED)
        thread.join(timeout=10)
        response.close()
        # The handler notices the dead socket on its next write and releases the
        # subscription; a leak here means an hour-long run accumulates one
        # bounded queue per browser refresh.
        bus.publish(EventKind.TURN_STARTED, turn=1)
        for _ in range(200):
            if bus.subscribers == 0:
                break
            bus.publish(EventKind.TURN_STARTED, turn=1)
            threading.Event().wait(0.02)
        assert bus.subscribers == 0

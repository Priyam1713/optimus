"""REST for control, SSE for the stream.

apex §7 says "REST/WS core". This is REST and **SSE**, not WebSocket, and the
difference is named rather than glossed (house rule 5).

The reason is the dependency line. Optimus's entire runtime dependency list is
`cryptography`; the Gate, the ledger and the context planes import nothing else,
and litellm is loaded lazily so that a harness which never calls a model never
pays for it. Adding an ASGI server and a WebSocket stack to get a live view
would make the whole project need them. SSE needs nothing — it is text over a
long-lived HTTP response, it is what AG-UI already specifies as its default
transport, and browsers reconnect it for free. Hand-rolling RFC 6455 framing to
be able to write "WS" in a document would be a rebuild of a solved thing, done
worse, for a word.

What SSE genuinely gives up is a client-to-server channel on the same socket.
That is why steering here is ordinary `POST`, which is a perfectly good reverse
channel and one that `curl` can drive.

## This port can steer an agent

So it is not a debug endpoint. It binds loopback, it requires a bearer token,
and binding anywhere else takes an explicit argument. An unauthenticated socket
that can inject instructions into a running agent is a remote code execution
path wearing a monitoring hat.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from optimus.surface.agui import AGUIEmitter, sse
from optimus.surface.control import Control, SteerKind
from optimus.surface.events import Bus

__all__ = ["SurfaceServer"]

#: How long a stream waits for an event before emitting a keep-alive comment.
#: Proxies and browsers drop an idle connection, and a 9B model thinking for
#: ninety seconds is an idle connection.
_KEEPALIVE_S = 15.0


class SurfaceServer:
    """A read-and-steer HTTP surface over one run.

    ```
    GET  /healthz                 liveness, the only unauthenticated route
    GET  /run                     a snapshot: run id, turn, pending steers
    GET  /events                  SSE, AG-UI events by default
    GET  /events?format=optimus   SSE, the raw bus events instead
    POST /steer  {kind, text}     enqueue / guide / interrupt
    POST /cancel {reason}         stop the run
    ```
    """

    def __init__(
        self,
        bus: Bus,
        control: Control | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str = "",
        allow_non_loopback: bool = False,
        state: Callable[[], dict[str, Any]] | None = None,
    ):
        if host not in ("127.0.0.1", "::1", "localhost") and not allow_non_loopback:
            raise ValueError(
                f"refusing to bind {host}: this port can inject instructions into a "
                "running agent. Pass allow_non_loopback=True if that is genuinely "
                "what you want, and put a token on it."
            )
        self.bus = bus
        self.control = control
        #: Generated rather than optional. A caller who wants no auth has to
        #: pass one and share it; there is no code path that serves without.
        self.token = token or secrets.token_urlsafe(24)
        self._state = state
        self._server = ThreadingHTTPServer((host, port), self._handler())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> SurfaceServer:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="optimus-surface", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Idempotent, and safe on a server that was never started.

        `BaseServer.shutdown()` blocks until `serve_forever()` acknowledges it,
        and `serve_forever()` is what sets that flag — so calling it on a socket
        that was bound but never served waits for an acknowledgement that has
        nobody to send it, forever. Binding in `__init__` and serving in
        `start()` is what makes that reachable, and it is worth keeping: the
        port has to be known before the run starts so it can be printed.
        """
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> SurfaceServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- the handler ----------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "optimus"

            def log_message(self, *_args: Any) -> None:
                """Silence. The ledger is the record; stderr is not."""

            # -- helpers ------------------------------------------------------

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                offered = header[7:] if header.startswith("Bearer ") else ""
                if not offered:
                    offered = parse_qs(
                        urlparse(self.path).query
                    ).get("token", [""])[0]
                # Constant-time: a token comparison that returns early leaks its
                # own prefix to anyone willing to time it.
                return secrets.compare_digest(offered, outer.token)

            def _json(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return {}

            # -- routes -------------------------------------------------------

            def do_GET(self) -> None:
                route = urlparse(self.path).path
                if route == "/healthz":
                    self._json(200, {"ok": True})
                    return
                if not self._authorized():
                    self._json(401, {"error": "a bearer token is required"})
                    return
                if route == "/run":
                    self._json(200, self._snapshot())
                elif route == "/events":
                    self._stream()
                else:
                    self._json(404, {"error": f"no route {route}"})

            def do_POST(self) -> None:
                route = urlparse(self.path).path
                if not self._authorized():
                    self._json(401, {"error": "a bearer token is required"})
                    return
                if outer.control is None:
                    self._json(409, {"error": "this run has no control plane"})
                    return
                body = self._body()
                if route == "/steer":
                    kind = str(body.get("kind", "guidance")).upper()
                    text = str(body.get("text", "")).strip()
                    if not text:
                        self._json(400, {"error": "a steer needs text"})
                        return
                    if kind not in SteerKind.__members__:
                        self._json(400, {
                            "error": f"unknown kind {kind.lower()!r}",
                            "known": [k.lower() for k in SteerKind.__members__],
                        })
                        return
                    steer = outer.control.send_kind(
                        SteerKind[kind], text, source="http"
                    )
                    self._json(202, {"accepted": steer.as_dict(),
                                     "pending": outer.control.pending})
                elif route == "/cancel":
                    outer.control.cancel(
                        str(body.get("reason", "")) or "cancelled over http",
                        source="http",
                    )
                    self._json(202, {"cancelled": True})
                else:
                    self._json(404, {"error": f"no route {route}"})

            # -- the stream ---------------------------------------------------

            def _snapshot(self) -> dict[str, Any]:
                body: dict[str, Any] = {
                    "run_id": outer.bus.run_id,
                    "subscribers": outer.bus.subscribers,
                }
                if outer.control is not None:
                    conf = outer.control.confidence
                    body.update({
                        "pending_steers": outer.control.pending,
                        "cancelled": outer.control.cancelled,
                        "confidence": (
                            {"value": conf.value, "source": conf.source,
                             "reason": conf.reason}
                            if conf else None
                        ),
                    })
                if outer._state is not None:
                    try:
                        body["state"] = outer._state()
                    except Exception as exc:
                        body["state_error"] = str(exc)
                return body

            def _stream(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                raw = query.get("format", ["agui"])[0] == "optimus"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                # Chunked would need framing; SSE is a stream that simply never
                # ends, so no length is announced and none is expected.
                self.end_headers()

                emitter = AGUIEmitter(run_id=outer.bus.run_id)
                with outer.bus.listen("http") as sub:
                    while True:
                        event = sub.get(timeout=_KEEPALIVE_S)
                        try:
                            if event is None:
                                if sub.closed:
                                    return
                                # A comment frame. Keeps proxies from reaping a
                                # connection while the model is thinking.
                                self.wfile.write(b": keep-alive\n\n")
                                self.wfile.flush()
                                continue
                            if raw:
                                self.wfile.write(sse(event.as_dict()))
                            else:
                                for out in emitter.translate(event):
                                    self.wfile.write(sse(out))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ValueError):
                            # The browser navigated away. Not an error.
                            return

        return Handler

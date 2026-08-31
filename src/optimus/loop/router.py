"""The router: local first, remote only by consent, and every choice recorded.

Implements the same `LLM` protocol the loop already speaks, so nothing upstream
learns that routing exists. What it adds over calling one model by name:

* **Local-first**, structurally. `Registry.candidates` refuses to return a remote
  engine unless `allow_remote` was set, so a request cannot leave the machine by
  omission.
* **Runtime truth.** A configured engine that fails its health check is skipped,
  not attempted — Achilles's rule, and the reason a stale manifest degrades into
  a slower route rather than a failed run.
* **Ordered fallback.** Candidates are tried in order and every failure is
  carried forward, so an exhausted router reports all of them rather than only
  the last.
* **The route is on the record.** A `model.route` event names the engine and
  model that served the turn and every candidate that did not, which is what
  makes "which model produced this trajectory" answerable from the ledger rather
  than from memory.

**Fatal failures do not fall through.** A 400 for a malformed request will be a
400 on every other candidate too, so trying the rest just multiplies the wait.
Only transient failures move to the next candidate — the same classification the
loop uses for its own retries (`llm.classify_error`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .engines import Candidate, ConfigError, Registry
from .llm import LiteLLM, ModelReply

#: Called with (kind, payload) to put a row on the ledger. Optional so the
#: router stays usable in a bare script.
Recorder = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class HealthResult:
    ok: bool
    detail: str = ""


def check_health(candidate: Candidate, *, timeout_s: float = 5.0) -> HealthResult:
    """Is this engine actually up?

    A HEAD/GET against the declared health URL, or `/models` when none is given.
    Deliberately cheap and deliberately not cached for long: the point is to know
    the state now, and a local server that sleeps on idle (llama.cpp's
    `--sleep-idle-seconds`) legitimately changes state between turns.
    """
    import urllib.error
    import urllib.request

    url = candidate.engine.health_url or (
        candidate.engine.base_url.rstrip("/") + "/models"
    )
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        # A 4xx from a health endpoint still proves something is listening.
        return HealthResult(exc.code < 500, f"HTTP {exc.code}")
    except Exception as exc:
        return HealthResult(False, f"{type(exc).__name__}: {exc}")
    return HealthResult(200 <= code < 400, f"HTTP {code}")


@dataclass
class RoutedLLM:
    """An `LLM` that picks its engine, local ones first."""

    registry: Registry
    #: The consent. False means this object cannot reach a hosted API at all.
    allow_remote: bool = False
    #: Pin to one declared model id. Empty means "route".
    model_id: str = ""
    temperature: float = 0.0
    num_retries: int = 2
    record: Recorder | None = None
    health_timeout_s: float = 5.0
    #: Set once a candidate has served a turn, so later turns do not re-probe
    #: health on the happy path. Cleared when that candidate fails.
    _pinned: Candidate | None = field(default=None, repr=False)
    _clients: dict[str, LiteLLM] = field(default_factory=dict, repr=False)

    # -- LLM protocol ---------------------------------------------------------

    @property
    def model(self) -> str:
        if self._pinned is not None:
            return self._pinned.label
        candidates, _ = self._candidates()
        return candidates[0].label if candidates else "unrouted"

    @property
    def wants_cache_hints(self) -> bool:
        return False

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        candidates, excluded = self._candidates()
        if not candidates:
            return self._unroutable(excluded)

        # A candidate that already worked goes first; the rest stay as fallback.
        if self._pinned is not None:
            candidates = [self._pinned] + [
                c for c in candidates if c.label != self._pinned.label
            ]

        failures: list[str] = []
        # Seeded, because every candidate can be skipped without one ever being
        # called — which is exactly what happens when the local server is asleep
        # and nothing else is declared.
        reply = ModelReply(
            error="no candidate was reachable", retryable=True,
            finish_reason="unreachable",
        )
        for candidate in candidates:
            if candidate.label != getattr(self._pinned, "label", None):
                health = check_health(candidate, timeout_s=self.health_timeout_s)
                if not health.ok:
                    failures.append(f"{candidate.label}: unhealthy ({health.detail})")
                    continue

            reply = self._client(candidate).complete(messages, tools)
            if not reply.error:
                if self._pinned is None or self._pinned.label != candidate.label:
                    self._emit_route(candidate, failures, excluded)
                self._pinned = candidate
                return reply

            failures.append(f"{candidate.label}: {reply.error}")
            if self._pinned is not None and self._pinned.label == candidate.label:
                self._pinned = None
            if not reply.retryable:
                # A malformed request is malformed everywhere. Falling through
                # would spend the wall clock to collect the same refusal again.
                return self._exhausted(reply, failures, excluded, fatal=True)

        return self._exhausted(reply, failures, excluded, fatal=False)

    # -- pieces ---------------------------------------------------------------

    def _candidates(self) -> tuple[list[Candidate], list[str]]:
        return self.registry.candidates(
            allow_remote=self.allow_remote, model_id=self.model_id, needs_tools=True
        )

    def _client(self, candidate: Candidate) -> LiteLLM:
        cached = self._clients.get(candidate.label)
        if cached is not None:
            return cached
        client = LiteLLM(
            model_name=candidate.litellm_model,
            temperature=self.temperature,
            max_output_tokens=candidate.model.max_output_tokens,
            num_retries=self.num_retries,
            timeout_s=candidate.engine.timeout_s,
            api_base=candidate.engine.base_url or None,
            api_key=candidate.engine.api_key(),
        )
        self._clients[candidate.label] = client
        return client

    def _emit_route(
        self, candidate: Candidate, failures: Sequence[str], excluded: Sequence[str]
    ) -> None:
        if self.record is None:
            return
        self.record(
            "model.route",
            {
                "engine": candidate.engine.id,
                "model": candidate.model.id,
                "runtime_model": candidate.model.runtime_id,
                "local": candidate.engine.local,
                "allow_remote": self.allow_remote,
                "fallbacks": list(failures),
                "excluded": list(excluded),
            },
        )

    def _unroutable(self, excluded: Sequence[str]) -> ModelReply:
        detail = "; ".join(excluded) or "no models declared"
        message = f"no routable model: {detail}"
        if self.record is not None:
            self.record("model.unroutable", {"excluded": list(excluded)})
        # Fatal: no amount of waiting adds a model to the manifest.
        return ModelReply(error=message, retryable=False, finish_reason="unrouted")

    def _exhausted(
        self,
        last: ModelReply,
        failures: Sequence[str],
        excluded: Sequence[str],
        *,
        fatal: bool,
    ) -> ModelReply:
        if self.record is not None:
            self.record(
                "model.route_exhausted",
                {"failures": list(failures), "excluded": list(excluded), "fatal": fatal},
            )
        return ModelReply(
            error="every candidate failed: " + " | ".join(failures),
            # The route is exhausted, but the *reason* is what the loop keys on:
            # a transient exhaustion is still worth waiting out.
            retryable=last.retryable and not fatal,
            retry_after_s=last.retry_after_s,
            usage=last.usage,
            finish_reason="route_exhausted",
        )


def build_router(
    *,
    config: str | None = None,
    allow_remote: bool = False,
    model_id: str = "",
    record: Recorder | None = None,
    temperature: float = 0.0,
) -> RoutedLLM:
    """Load a manifest and return a router over it."""
    return RoutedLLM(
        registry=Registry.load(config),
        allow_remote=allow_remote,
        model_id=model_id,
        record=record,
        temperature=temperature,
    )


def live_models(registry: Registry, *, timeout_s: float = 5.0) -> dict[str, list[str]]:
    """Ask each healthy local engine what it is actually serving.

    Reporting only. It deliberately does not write anything back into the
    manifest: a router that invents its own config cannot be reviewed, and a
    model discovered this way has no declared context size or tool support to
    route on.
    """
    import json
    import urllib.request

    found: dict[str, list[str]] = {}
    for engine in registry.engines.values():
        ok, _ = engine.usable()
        if not ok or not engine.base_url:
            continue
        try:
            url = engine.base_url.rstrip("/") + "/models"
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                payload = json.load(response)
        except Exception:
            continue
        found[engine.id] = sorted(
            str(m.get("id")) for m in payload.get("data", []) if m.get("id")
        )
    return found


__all__ = [
    "ConfigError",
    "HealthResult",
    "Recorder",
    "RoutedLLM",
    "build_router",
    "check_health",
    "live_models",
]

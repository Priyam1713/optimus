"""The model call, and the only place a token number is invented.

Two jobs, and the second is the one that matters for row 19 of
[apex.md](../../../apex.md).

**Mount the provider layer.** `litellm` already speaks to every provider, already
normalises usage, and already carries a per-model price table. Re-implementing
that would be exactly the mistake the project's own selection rule names: mount
what exists and spend the effort on the layer that does not. The `LLM` protocol
below is one method wide so the rest of the loop never learns which provider it
is talking to.

**Stop guessing at tokens.** `heuristic_tokens` (~4 chars) is documented as good
enough for budgeting and never for billing, and `STATUS.md` carries it as a
limitation. Since the whole published claim is *tokens per solved task*, a
metric derived from a 4-chars-per-token guess would be a number about our
arithmetic rather than about our harness. So:

* **Billing comes from the provider.** `Usage` is read off the response, never
  computed. If the provider does not report it, the field is zero and stays
  zero — an absent number is honest and an estimated one contaminates the
  headline.
* **Budgeting uses the model's real tokenizer** via `litellm.token_counter`,
  which is what `ContextWindow` should be handed instead of the heuristic.

Cache reads are tracked separately because they are the difference between a
20-turn conversation costing O(n) and O(n²), and because a harness that reports
`input_tokens` without saying how many were cache hits is reporting a number its
reader cannot compare to anyone else's.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

#: Providers whose APIs take explicit cache breakpoints rather than caching by
#: prefix automatically. Membership decides whether `cache_control` markers are
#: worth emitting; emitting them elsewhere is inert, not harmful.
_EXPLICIT_CACHE_HINTS = ("claude", "anthropic")


@dataclass(frozen=True, slots=True)
class Usage:
    """What the call cost, as the provider reported it."""

    input_tokens: int = 0
    output_tokens: int = 0
    #: Subset of `input_tokens` served from cache. Not double counted.
    cached_tokens: int = 0
    cost_usd: float = 0.0
    wall_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merged(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            wall_ms=self.wall_ms + other.wall_ms,
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    #: Set when the provider failed and the loop should see it as an
    #: observation rather than an exception.
    error: str = ""
    #: True when the failure was transient — a rate limit, a 503, a dropped
    #: connection. A run should wait those out; it should not wait out a 404 for
    #: a model that does not exist.
    retryable: bool = False
    #: What the provider asked us to wait, when it said. 0.0 means it did not,
    #: and the loop falls back to its own exponential backoff.
    retry_after_s: float = 0.0

    @property
    def failed(self) -> bool:
        return bool(self.error)

    @property
    def acted(self) -> bool:
        """Did this turn do anything at all?"""
        return bool(self.tool_calls)

    @property
    def idled(self) -> bool:
        """A no-action turn: the model answered, and chose to do nothing.

        Deliberately *not* `not acted`. A call that never reached the provider
        did not idle — it failed, and counting it as an idle turn corrupts the
        one metric this project exists to publish (`research.md` §2.1, Goose
        0.2-0.3/task against OpenCode's 2.0-2.16). A real run made exactly this
        mistake visible: three rate-limited calls were reported as three
        no-action turns when the model had not idled once.
        """
        return not self.error and not self.tool_calls


class LLM(Protocol):
    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply: ...

    @property
    def model(self) -> str: ...


# --------------------------------------------------------------------------
# tokenizing
# --------------------------------------------------------------------------

def token_counter_for(model: str) -> Callable[[str], int]:
    """The model's real tokenizer, falling back loudly rather than silently.

    Returned counter is what `ContextWindow` should be constructed with, so
    compaction decisions are made in the same units the bill is denominated in.
    """
    try:
        import litellm
    except ImportError:  # pragma: no cover - litellm is a hard dep of the loop
        from ..context.episodes import heuristic_tokens

        return heuristic_tokens

    def count(text: str) -> int:
        try:
            return max(1, int(litellm.token_counter(model=model, text=text)))
        except Exception:
            from ..context.episodes import heuristic_tokens

            return heuristic_tokens(text)

    return count


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

def _extract_usage(response: Any, model: str, wall_ms: int) -> Usage:
    """Read usage off the response. Compute nothing that was reported."""
    raw = getattr(response, "usage", None) or {}
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)

    prompt = int(get("prompt_tokens", 0) or 0)
    completion = int(get("completion_tokens", 0) or 0)

    details = get("prompt_tokens_details", None) or {}
    dget = details.get if isinstance(details, dict) else lambda k, d=None: getattr(details, k, d)
    cached = int(dget("cached_tokens", 0) or 0)

    cost = 0.0
    try:
        import litellm

        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        # A missing price is a missing price. Guessing one would put a number in
        # the receipt that no invoice will ever match.
        cost = 0.0

    return Usage(
        input_tokens=prompt,
        output_tokens=completion,
        cached_tokens=cached,
        cost_usd=cost,
        wall_ms=wall_ms,
    )


#: Exception class names that mean "try again later". Matched by name so that
#: litellm's exception hierarchy can move without silently reclassifying every
#: failure as fatal — the direction that would abandon a run over a hiccup.
_RETRYABLE_NAMES = frozenset({
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    "APIConnectionError",
    "APIError",
    "Timeout",
    "APITimeoutError",
})

#: And these mean "stop now". Retrying a model id that does not exist, or a key
#: that is not valid, burns the wall clock to arrive at the same answer — which
#: is what a real run did: three attempts, ten minutes, one 404.
_FATAL_NAMES = frozenset({
    "NotFoundError",
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "UnprocessableEntityError",
    "ContextWindowExceededError",
})


#: Providers say how long to wait, in several shapes. Guessing when they have
#: told you is how a run either hammers a rate limit or sleeps far longer than
#: it needed to.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry in ([\d.]+)\s*s", re.I),
    re.compile(r'"retryDelay"\s*:\s*"([\d.]+)s"', re.I),
    re.compile(r"retry[-_ ]after[\"']?\s*[:=]\s*[\"']?([\d.]+)", re.I),
)


def retry_after_seconds(exc: BaseException) -> float:
    """How long the provider asked us to wait, or 0.0 when it did not say."""
    for attribute in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    text = str(exc)
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:  # pragma: no cover - regex guarantees a number
                continue
    return 0.0


def classify_error(exc: BaseException) -> bool:
    """True when waiting is likely to help."""
    name = type(exc).__name__
    if name in _FATAL_NAMES:
        return False
    if name in _RETRYABLE_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        # 429 and 5xx are worth waiting out; 4xx otherwise is not.
        return status == 429 or status >= 500
    # Unknown failures are treated as transient, because giving up on a run is
    # the more expensive mistake and the error budget bounds it anyway.
    return True


def _parse_tool_calls(message: Any) -> tuple[ToolCall, ...]:
    import json

    raw = getattr(message, "tool_calls", None) or []
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw):
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) or ""
        args_raw = getattr(fn, "arguments", None) or "{}"
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw or "{}")
            except (json.JSONDecodeError, TypeError):
                # A model that emitted unparseable arguments has still *acted*.
                # Surfacing the raw text lets the loop hand back a real error
                # instead of silently treating the turn as idle.
                args = {"__unparsed__": args_raw}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {"__unparsed__": str(args_raw)}
        if not isinstance(args, dict):
            args = {"__unparsed__": str(args)}
        calls.append(
            ToolCall(
                call_id=str(getattr(tc, "id", None) or f"call_{i + 1}"),
                name=str(name),
                arguments=args,
            )
        )
    return tuple(calls)


@dataclass
class LiteLLM:
    """One provider-agnostic call, with retries and cache hints."""

    model_name: str
    temperature: float = 0.0
    max_output_tokens: int = 8_000
    num_retries: int = 3
    timeout_s: float = 600.0
    api_base: str | None = None
    api_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def wants_cache_hints(self) -> bool:
        low = self.model_name.lower()
        return any(hint in low for hint in _EXPLICIT_CACHE_HINTS)

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": list(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "num_retries": self.num_retries,
            "timeout": self.timeout_s,
            **self.extra,
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key

        started = time.monotonic()
        try:
            response = litellm.completion(**kwargs)
        except Exception as exc:
            # `num_retries` already exhausted its attempts. Returning this as a
            # reply rather than raising keeps a provider hiccup from destroying a
            # whole benchmark trial: the loop records it, tells the model, and
            # carries on with one turn spent.
            return ModelReply(
                error=f"{type(exc).__name__}: {exc}",
                retryable=classify_error(exc),
                retry_after_s=retry_after_seconds(exc),
                usage=Usage(wall_ms=int((time.monotonic() - started) * 1000)),
                finish_reason="error",
            )
        wall_ms = int((time.monotonic() - started) * 1000)

        choice = response.choices[0]
        message = choice.message
        return ModelReply(
            text=(getattr(message, "content", None) or ""),
            reasoning=(getattr(message, "reasoning_content", None) or ""),
            tool_calls=_parse_tool_calls(message),
            usage=_extract_usage(response, self.model_name, wall_ms),
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        )


@dataclass
class ScriptedLLM:
    """A model that says exactly what it was told to say.

    Used by the tests, and by anyone who wants to exercise the loop's breakers,
    metering and ledger without spending money or requiring a network. It is the
    only way to assert that the no-action breaker fires, because a real model
    cannot be made to stall on demand.
    """

    replies: list[ModelReply]
    model_name: str = "scripted/none"
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.model_name

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> ModelReply:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            return ModelReply(text="(script exhausted)", finish_reason="stop")
        return self.replies.pop(0)

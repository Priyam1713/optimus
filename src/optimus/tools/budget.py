"""Choosing how tools are presented, by budget rather than by config flag.

`research.md` §4.4 settles the economics and then immediately complicates them:

* Code execution against tool APIs cut one Anthropic workflow from 150,000 to
  2,000 tokens (98.7%), and collapsed Cloudflare's ~244,000-token API surface to
  ~1,000.
* Claude Code defers tool loading once MCP descriptions exceed **10% of the
  context budget**.
* And code mode is *worse* for three or four tools that rarely chain, where
  discovery-inspect-execute adds latency for nothing.

Meanwhile Goose wins its 20-40x token advantage partly by doing the opposite —
eagerly pre-injecting structure. Greedy selection cannot hold both facts
(`apex.md` §1.1); a policy can, because the right answer depends on the shape of
the task and the size of the surface, which are both measurable at the moment of
choosing.

So this is the synthesis: three modes, one decision function, no flag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Sequence

from ..context.episodes import TokenCounter, heuristic_tokens
from ..context.window import ContextBudget


class ToolMode(StrEnum):
    #: Full schemas in context. Cheapest when the surface is small.
    DIRECT = "direct"
    #: Names and one-line descriptions; schemas fetched on demand.
    SEARCH = "search"
    #: Typed facades in a persistent interpreter; the model writes code.
    CODE = "code"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]

    def render_full(self) -> str:
        return f'{{"tool": "{self.name}", "args": {json.dumps(self.schema, sort_keys=True)}}}  -- {self.description}'

    def render_brief(self) -> str:
        return f"{self.name} -- {self.description[:80]}"


@dataclass(frozen=True, slots=True)
class ToolDecision:
    mode: ToolMode
    reason: str
    schema_tokens: int
    allowance: int
    rendered: str

    def render(self) -> str:
        return (
            f"{self.mode}: {self.reason} "
            f"(schemas {self.schema_tokens:,} tok vs allowance {self.allowance:,})"
        )


@dataclass(frozen=True, slots=True)
class ToolBudgetPolicy:
    budget: ContextBudget
    #: Claude Code's threshold, adopted rather than invented.
    tool_share: float = 0.10
    #: Chained calls above which writing code beats calling tools one at a time.
    chain_threshold: int = 3

    @property
    def allowance(self) -> int:
        return int(self.budget.fillable * self.tool_share)

    def choose(
        self,
        specs: Sequence[ToolSpec],
        *,
        expected_calls: int = 1,
        count_tokens: TokenCounter = heuristic_tokens,
    ) -> ToolDecision:
        full = "\n".join(s.render_full() for s in specs)
        schema_tokens = count_tokens(full) if specs else 0
        allowance = self.allowance

        fits = schema_tokens <= allowance
        chaining = expected_calls >= self.chain_threshold

        if fits and not chaining:
            return ToolDecision(
                ToolMode.DIRECT,
                "surface fits the allowance and the task is not chained",
                schema_tokens, allowance, full,
            )
        if chaining and len(specs) > 1:
            return ToolDecision(
                ToolMode.CODE,
                f"{expected_calls} chained calls: one program beats {expected_calls} round trips",
                schema_tokens, allowance,
                _render_facades(specs),
            )
        if fits:
            return ToolDecision(
                ToolMode.DIRECT,
                "chained, but only one tool is available to chain",
                schema_tokens, allowance, full,
            )
        return ToolDecision(
            ToolMode.SEARCH,
            f"surface exceeds {self.tool_share:.0%} of the fillable budget; deferring schemas",
            schema_tokens, allowance,
            "\n".join(s.render_brief() for s in specs),
        )


def _render_facades(specs: Iterable[ToolSpec]) -> str:
    """Typed stubs the model writes against instead of tool-call JSON.

    Generated from the same specs the direct mode renders, so the callable
    surface and the described surface cannot drift — Achilles's rule about
    deriving the action schema from the registry (`audit.md` §3.7), applied to
    the other direction.
    """
    lines = ["# facades available in this session's kernel"]
    for s in specs:
        args = ", ".join(f"{k}: {_py_type(v)}" for k, v in sorted(s.schema.items()))
        lines.append(f"def {s.name}({args}) -> dict: ...  # {s.description[:70]}")
    return "\n".join(lines)


def _py_type(example: Any) -> str:
    match example:
        case bool():
            return "bool"
        case int():
            return "int"
        case float():
            return "float"
        case list():
            return "list"
        case dict():
            return "dict"
        case _:
            return "str"

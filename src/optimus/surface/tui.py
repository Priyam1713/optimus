"""The run, on a terminal.

Line-oriented ANSI rather than `curses`, for a boring and decisive reason:
`curses` is not in the Windows standard library, and this is a project developed
on Windows 11. A TUI that cannot start on the author's own machine is not a
surface. Appending lines also means the output survives being piped to a file or
scrolled back, which a full-screen redraw does not.

Colour degrades rather than breaks: `NO_COLOR` in the environment, or a
non-tty stdout, drops the escape codes and leaves the same text.

This renderer is a *subscriber*, so it is subject to the bus's drop policy — if
the terminal cannot keep up with the loop, events are lost. It says so at the
end rather than presenting a gap-free-looking picture, which is the whole reason
`Subscription.dropped` exists.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, TextIO

from optimus.surface.events import Bus, EventKind, RunEvent, Subscription

__all__ = ["TUI"]

_RESET = "\x1b[0m"
_STYLES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
}


class TUI:
    """Renders bus events as they arrive."""

    def __init__(self, bus: Bus, *, stream: TextIO | None = None, color: bool | None = None):
        self.bus = bus
        self.stream = stream or sys.stdout
        if color is None:
            color = (
                not os.environ.get("NO_COLOR")
                and hasattr(self.stream, "isatty")
                and self.stream.isatty()
            )
        self.color = color
        self._thread: threading.Thread | None = None
        self._sub: Subscription | None = None
        self._turn = 0

    # -- styling --------------------------------------------------------------

    def _s(self, text: str, style: str) -> str:
        if not self.color:
            return text
        return f"{_STYLES.get(style, '')}{text}{_RESET}"

    def _write(self, line: str) -> None:
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (BrokenPipeError, ValueError):
            pass

    # -- running --------------------------------------------------------------

    def start(self) -> TUI:
        self._sub = self.bus.subscribe("tui")
        self._thread = threading.Thread(target=self._pump, name="optimus-tui", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._sub is not None:
            self._sub.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> TUI:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _pump(self) -> None:
        assert self._sub is not None
        for event in self._sub:
            line = self.render(event)
            if line:
                self._write(line)
        if self._sub.dropped:
            self._write(self._s(
                f"  ({self._sub.dropped} events dropped — this view was behind; "
                f"`optimus why` has all of them)", "dim",
            ))

    # -- rendering ------------------------------------------------------------

    def render(self, event: RunEvent) -> str:
        p: dict[str, Any] = event.payload
        match event.kind:
            case EventKind.RUN_STARTED:
                return (
                    self._s("▶ run ", "bold")
                    + self._s(event.run_id, "cyan")
                    + self._s(f"  model={p.get('model', '?')}"
                              f"  max_turns={p.get('limits', {}).get('max_turns', '?')}",
                              "dim")
                )

            case EventKind.TURN_STARTED:
                self._turn = event.turn
                return self._s(f"── turn {event.turn} " + "─" * 40, "dim")

            case EventKind.MODEL_CALL:
                meter = p.get("meter") or {}
                extra = meter.get("extra") or {}
                if p.get("error"):
                    return "  " + self._s(f"provider: {p['error'][:110]}", "red")
                tokens = meter.get("input_tokens", 0)
                cached = extra.get("cached_tokens", 0)
                share = f" ({cached / tokens:.0%} cached)" if tokens and cached else ""
                return "  " + self._s(
                    f"model {tokens:,} in / {meter.get('output_tokens', 0):,} out{share}",
                    "dim",
                )

            case EventKind.TOOL_CALL:
                return (
                    "  " + self._s("→ ", "blue")
                    + self._s(str(p.get("name", "?")), "bold")
                    + " " + self._s(str(p.get("brief", ""))[:90], "dim")
                )

            case EventKind.TOOL_RESULT:
                preview = str(p.get("preview", "")).strip().splitlines()
                head = preview[0][:90] if preview else ""
                if p.get("denied"):
                    return "  " + self._s("✗ refused ", "red") + self._s(head, "dim")
                code = p.get("exit_code")
                mark = "✓" if code in (0, None) else f"✗ exit {code}"
                style = "green" if code in (0, None) else "yellow"
                return "  " + self._s(mark + " ", style) + self._s(head, "dim")

            case EventKind.GATE_PARKED:
                return "  " + self._s(
                    f"⏸ parked for a human: {p.get('name', '')} — {p.get('reason', '')[:80]}",
                    "yellow",
                )

            case EventKind.GATE_DENIED:
                return "  " + self._s(
                    f"✗ gate: {p.get('verdict', '')} {p.get('reason', '')[:80]}", "red"
                )

            case EventKind.CONTEXT_TURN:
                estimated = p.get("estimated", 0)
                allowance = p.get("allowance", 0) or 1
                observed = p.get("observed_last", 0)
                # The one number four bugs were invisible without: what the
                # plane believed, next to what the provider actually charged.
                gap = ""
                if observed and estimated and observed > estimated:
                    gap = self._s(
                        f"  under-estimated by {observed - estimated:,}", "yellow"
                    )
                return "  " + self._s(
                    f"ctx {estimated:,}/{allowance:,} ({estimated / allowance:.0%})"
                    f"  billed_last={observed:,}", "dim",
                ) + gap

            case EventKind.CONTEXT_COMPACTED:
                return "  " + self._s(
                    f"⇊ compacted: {p.get('before', '?')} → {p.get('after', '?')} tokens",
                    "magenta",
                )

            case EventKind.BREAKER:
                return "  " + self._s(
                    f"! {p.get('kind', '')}: {p.get('detail', '')[:90]}", "yellow"
                )

            case EventKind.STEERED:
                return "  " + self._s(
                    f"↳ {p.get('kind', 'steer')} from {p.get('source', '?')}: "
                    f"{str(p.get('text', ''))[:80]}", "cyan",
                )

            case EventKind.RUN_FINISHED:
                stop = str(p.get("stop_reason", "?"))
                style = "green" if stop == "finished" else "yellow"
                return (
                    self._s("■ ", style)
                    + self._s(f"{stop}", "bold")
                    + self._s(
                        f"  turns={p.get('turns', 0)}"
                        f"  denials={p.get('gate_denials', 0)}"
                        f"  approvals_needed={p.get('approvals_required', 0)}"
                        f"  compactions={p.get('compactions', 0)}"
                        f"  {p.get('wall_ms', 0) / 1000:.0f}s",
                        "dim",
                    )
                    # The loop does not know whether the task was solved, and a
                    # surface that implies otherwise is inventing a result.
                    + self._s("  (solved: unknown — the verifier decides)", "dim")
                )

        return ""

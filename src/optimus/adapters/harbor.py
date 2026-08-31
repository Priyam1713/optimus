"""The Harbor adapter — one class, and the reason M0–M2 were built first.

[Harbor](https://github.com/harbor-framework/harbor) is the official
Terminal-Bench 2.0 harness and already hosts Terminus-2, Claude Code, Codex CLI,
Gemini CLI, OpenHands, Antigravity SDK, Grok Build and Mini-SWE-Agent. Appearing
next to them costs exactly one `BaseAgent` subclass::

    harbor run -d terminal-bench@2.0 \\
        --agent optimus.adapters.harbor:OptimusAgent \\
        -m anthropic/claude-sonnet-4-20250514

Everything interesting about this file is in what it *refuses* to do to make that
work.

**It does not grant itself permission.** Harbor runs unattended, and the Gate's
hard invariant parks every model-chosen mutation for a human. The adapter cannot
mint the assent that clears it — `gate/envelope.py` explains why that would be
Bellona's `--yolo` wearing better clothes. Instead it *loads* an owner-signed
envelope named by `OPTIMUS_ENVELOPE`, hands it to the Gate, and the Gate verifies
it against a fingerprint from `OPTIMUS_OWNER_FINGERPRINT`. With no envelope the
run still happens and still produces honest numbers — every mutation comes back
to the model as a refusal, the task fails, and `approvals_required` says exactly
how many interventions a human would have had to make. That is a legitimate row
on the board, and it is the row this harness scores when nobody authorised it.

**It does not decide whether it won.** `AgentContext` gets the token and cost
numbers; nothing here writes a reward. Harbor's verifier grades the container
after the loop exits, and `report.py` joins that verdict to our ledger.

**It does not pretend the Gate's local guarantees survived the trip.** The
container's filesystem gets `gate/remote.py`'s weaker, explicitly-labelled
resolution, and `Isolation.CONTAINER` is claimed only because Harbor genuinely
provides it.

What ships out of a trial, in `logs_dir`: a signed hash-chained ledger of every
decision and every command, an ATIF trajectory Harbor's own tooling can read, and
`optimus-metrics.json` carrying the four numbers nobody else on that board
prints — tokens per solved task, no-action turns, unsafe attempts refused, and
operator interventions required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import threading
from collections.abc import Sequence
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from ..context.window import ContextBudget, ContextWindow
from ..gate.envelope import Envelope, EnvelopeRefused
from ..gate.gate import Gate
from ..gate.policy import benchmark_policy
from ..gate.remote import RemoteResolver
from ..ledger.events import TrustLabel
from ..ledger.keys import AgentKey
from ..ledger.store import DurableChain, LedgerStore
from ..loop.agent import AgentLoop, LoopLimits, RunOutcome
from ..loop.engines import ConfigError, Registry
from ..loop.llm import LiteLLM, token_counter_for
from ..loop.router import RoutedLLM
from ..meter import aggregate
from ..tools.remote import RemoteTools, probe_environment
from ..venues.base import Isolation
from ..venues.remote import RemoteExec, RemoteVenue

try:  # pragma: no cover - exercised only where harbor is installed
    from harbor.agents.base import BaseAgent
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

    HARBOR_AVAILABLE = True
except ImportError:  # pragma: no cover
    HARBOR_AVAILABLE = False
    BaseAgent = object  # type: ignore[assignment,misc]
    BaseEnvironment = Any  # type: ignore[assignment,misc]
    AgentContext = Any  # type: ignore[assignment,misc]

METRICS_FILE = "optimus-metrics.json"
VERSION = "0.0.1"
AGENT_NAME = "optimus"

#: Backstop beyond the timeout we already asked the environment to honour. If the
#: environment's own timeout works, this never fires; if it does fire, the
#: command is reported as timed out rather than as an exit code it never had.
_TRANSPORT_GRACE_S = 60.0

#: How long to let a cancelled loop finish its turn and write its receipt.
_STOP_GRACE_S = 120.0


# --------------------------------------------------------------------------
# async environment -> synchronous transport
# --------------------------------------------------------------------------

class EnvironmentTransport:
    """Runs an argv in Harbor's environment, from a worker thread.

    Harbor's environment API is async and the rest of Optimus is not. Rather than
    colouring the Gate, the ledger, the context plane and the tool plane async
    for the sake of one caller, the loop runs in a thread and every command hops
    back onto the event loop through here.

    One subtlety worth naming: `environment.exec` already wraps what it is given
    in `bash -lc`. `RemoteResolver` also builds `(bash, -lc, script)`, because on
    a transport that took a real argv that is the honest representation. Sending
    both would double-wrap and quietly change the quoting, so this unwraps ours
    and lets the environment supply the shell.
    """

    def __init__(
        self,
        environment: BaseEnvironment,
        event_loop: asyncio.AbstractEventLoop,
        *,
        logger: logging.Logger | None = None,
    ):
        self._environment = environment
        self._loop = event_loop
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def to_command(argv: Sequence[str]) -> str:
        argv = list(argv)
        if len(argv) == 3 and argv[0] in ("bash", "sh") and argv[1] in ("-lc", "-c"):
            return argv[2]
        return shlex.join(argv)

    def __call__(
        self, argv: Sequence[str], *, cwd: str, timeout_s: float
    ) -> RemoteExec:
        command = self.to_command(argv)
        coroutine = self._environment.exec(
            command, cwd=cwd or None, timeout_sec=int(timeout_s)
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            result = future.result(timeout=timeout_s + _TRANSPORT_GRACE_S)
        except FutureTimeout:
            future.cancel()
            return RemoteExec(
                exit_code=124, stdout="", stderr="killed by the harness backstop",
                timed_out=True,
            )
        return RemoteExec(
            exit_code=int(result.return_code),
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            # 124 is the GNU `timeout` convention and what most container
            # runtimes surface. Not a guess about anything else.
            timed_out=int(result.return_code) == 124,
        )


# --------------------------------------------------------------------------
# trajectory
# --------------------------------------------------------------------------

def build_trajectory(
    outcome: RunOutcome,
    *,
    instruction: str,
    model_name: str | None,
    session_id: str | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Render the run as ATIF, as a plain dict.

    Built without importing Harbor's pydantic models so the same function works
    when Harbor is not installed — which is how the tests check the shape without
    dragging a container runtime into the suite. `write_trajectory` validates it
    against the real model when Harbor *is* present, so the two cannot drift.
    """
    steps: list[dict[str, Any]] = [
        {"step_id": 1, "source": "user", "message": instruction}
    ]
    for record in outcome.steps:
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": "agent",
            "timestamp": record.timestamp,
            "message": record.text or "",
            "llm_call_count": 1,
            "metrics": {
                "prompt_tokens": record.usage.input_tokens,
                "completion_tokens": record.usage.output_tokens,
                "cached_tokens": record.usage.cached_tokens or None,
                "cost_usd": record.usage.cost_usd or None,
            },
        }
        if model_name:
            step["model_name"] = model_name
        if record.reasoning:
            step["reasoning_content"] = record.reasoning
        if record.calls:
            step["tool_calls"] = [
                {
                    "tool_call_id": c.call_id,
                    "function_name": c.name,
                    "arguments": c.arguments,
                }
                for c in record.calls
            ]
        if record.results:
            step["observation"] = {
                "results": [
                    {"source_call_id": call_id, "content": content}
                    for call_id, content in record.results
                ]
            }
        if record.breaker or record.error:
            step["extra"] = {
                k: v for k, v in
                (("breaker", record.breaker), ("error", record.error)) if v
            }
        steps.append(step)

    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "agent": {
            "name": AGENT_NAME,
            "version": VERSION,
            "model_name": model_name,
            "extra": {"harness": "optimus", "stop_reason": outcome.stop_reason},
        },
        "steps": steps,
        "notes": (
            "Produced by the Optimus harness. `extra.optimus` carries the "
            "metrics this project exists to publish: tokens per solved task is "
            "this trajectory's tokens divided by the verifier's verdict, which "
            "the agent deliberately does not write for itself."
        ),
        "final_metrics": {
            "total_prompt_tokens": outcome.usage.input_tokens,
            "total_completion_tokens": outcome.usage.output_tokens,
            "total_cached_tokens": outcome.usage.cached_tokens or None,
            "total_cost_usd": outcome.usage.cost_usd or None,
            "total_steps": len(steps),
            "extra": extra,
        },
        "extra": {"optimus": extra},
    }


def write_trajectory(path: Path, trajectory: dict[str, Any]) -> None:
    """Write it, validating against Harbor's own model where that is possible."""
    payload = trajectory
    if HARBOR_AVAILABLE:  # pragma: no cover - needs harbor installed
        from harbor.models.trajectories import Trajectory

        payload = Trajectory.model_validate(trajectory).to_json_dict()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------
# the agent
# --------------------------------------------------------------------------

class OptimusAgent(BaseAgent):  # type: ignore[misc]
    """Optimus, as a Harbor agent."""

    SUPPORTS_ATIF: bool = True
    SUPPORTS_WINDOWS: bool = False

    def __init__(self, *args: Any, **kwargs: Any):
        if not HARBOR_AVAILABLE:  # pragma: no cover
            raise ImportError(
                "harbor is not installed; `pip install 'optimus[harbor]'` to run "
                "the adapter, or import the loop directly for a local run"
            )
        super().__init__(*args, **kwargs)
        self._outcome: RunOutcome | None = None
        self._metrics: dict[str, Any] = {}
        self._envelope_id: str | None = None
        self._stop = threading.Event()
        self._workspace: str = "/app"

    # -- identity -------------------------------------------------------------

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str | None:
        return VERSION

    # -- setup ----------------------------------------------------------------

    async def setup(self, environment: BaseEnvironment) -> None:
        """Establish the workspace and confirm the transport's assumptions.

        Nothing is installed into the environment. The loop runs on the host and
        reaches in through `exec`, so the only thing worth checking is that the
        two commands the file tools are built on actually exist — because
        discovering that `base64` is absent on turn 14 of a trial reads as a
        model failure and is not one.
        """
        self._workspace = await self._resolve_workspace(environment)
        probe = await environment.exec(
            "command -v base64 >/dev/null && command -v bash >/dev/null && echo ok",
            timeout_sec=30,
        )
        if (probe.stdout or "").strip() != "ok":
            self.logger.warning(
                "environment lacks bash or base64; read_file/write_file will fail "
                "and the agent will have to fall back to shell redirection"
            )

    async def _resolve_workspace(self, environment: BaseEnvironment) -> str:
        configured = getattr(
            getattr(environment, "task_env_config", None), "workdir", None
        )
        if configured:
            return str(configured)
        result = await environment.exec("pwd", timeout_sec=30)
        return ((result.stdout or "").strip() or "/").splitlines()[0]

    # -- run ------------------------------------------------------------------

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self._workspace:  # pragma: no cover - setup always runs first
            self._workspace = await self._resolve_workspace(environment)

        event_loop = asyncio.get_running_loop()
        transport = EnvironmentTransport(environment, event_loop, logger=self.logger)

        # `asyncio.to_thread` cannot be cancelled, so the loop is given an
        # Event and asked to stop at its next turn boundary. Without this a
        # trial Harbor has already timed out keeps a local GPU busy for as long
        # as the loop's own ceiling allows — which is what happened on a real
        # run: Harbor recorded a timeout at 900s and the loop ran on to 1800s.
        task = asyncio.create_task(
            asyncio.to_thread(self._run_blocking, instruction, transport)
        )
        try:
            outcome, metrics, trajectory = await task
        except asyncio.CancelledError:
            self._stop.set()
            self.logger.warning(
                "cancelled; asked the loop to stop and waiting for it to write "
                "its receipt"
            )
            # Shielded: the thread is already running and cannot be killed, so
            # the only choice is whether to *wait* for its receipt or abandon it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_GRACE_S)
            raise
        self._outcome, self._metrics = outcome, metrics

        # Populated as the run *ends* rather than as it goes, which is a real
        # limitation: a trial killed by Harbor's own timeout loses these numbers
        # even though the ledger on disk still has every row. Streaming them
        # needs the loop to call back per turn — an M4 item, and named here
        # rather than papered over.
        context.n_input_tokens = outcome.usage.input_tokens
        context.n_output_tokens = outcome.usage.output_tokens
        context.n_cache_tokens = outcome.usage.cached_tokens
        context.cost_usd = outcome.usage.cost_usd or None
        context.metadata = {**(context.metadata or {}), "optimus": metrics}

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        write_trajectory(self.logs_dir / "trajectory.json", trajectory)
        (self.logs_dir / METRICS_FILE).write_text(
            json.dumps(metrics, indent=2, default=str), encoding="utf-8"
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Rebuild the receipt from the ledger after `run()` is over.

        Harbor calls this once the trial's logs are back on the host, including
        when `run()` was killed by the agent timeout — which is exactly when it
        earns its place. A real local trial was cut off at 900 seconds mid-turn
        and lost every number, even though the signed ledger on disk still held
        all 233KB of them, because the metrics were only written on the way out
        of `run()`.

        That the whole receipt can be rebuilt from the ledger alone is invariant
        2 (`apex.md` §3) paying for itself: the Ledger is the system of record
        and the metrics file is a projection, so losing the projection loses
        nothing.
        """
        ledger_path = self.logs_dir / "ledger.db"
        if not ledger_path.is_file():
            return
        if self._metrics and (self.logs_dir / METRICS_FILE).is_file():
            return  # `run()` finished and already wrote it

        try:
            store = LedgerStore(ledger_path)
        except Exception as exc:  # pragma: no cover - a corrupt ledger
            self.logger.warning(f"cannot reopen the ledger to rebuild metrics: {exc}")
            return
        try:
            run_id = next(
                (
                    e.payload["run_id"]
                    for e in store.events()
                    if e.payload.get("run_id")
                ),
                self.session_id or "",
            )
            metrics = self._metrics_for(store, run_id)
        finally:
            store.close()

        self._metrics = metrics
        context.n_input_tokens = metrics["input_tokens"]
        context.n_output_tokens = metrics["output_tokens"]
        context.n_cache_tokens = metrics["cached_tokens"]
        context.cost_usd = metrics["cost_usd"] or None
        context.metadata = {**(context.metadata or {}), "optimus": metrics}

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / METRICS_FILE).write_text(
            json.dumps(metrics, indent=2, default=str), encoding="utf-8"
        )
        self.logger.info(
            f"rebuilt the receipt from {metrics['ledger_rows']} ledger rows "
            "after the run was cut short"
        )

    # -- the synchronous half -------------------------------------------------

    def _run_blocking(
        self, instruction: str, transport: EnvironmentTransport
    ) -> tuple[RunOutcome, dict[str, Any], dict[str, Any]]:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        run_id = self.session_id or f"{AGENT_NAME}-run"

        venue = RemoteVenue(
            transport, name="harbor", isolation=Isolation.CONTAINER
        )
        model_name = self.model_name or os.environ.get("OPTIMUS_MODEL", "")

        # The ledger ships with the trial. A receipt kept somewhere else is a
        # receipt nobody will ever line up against the result.
        store = LedgerStore(self.logs_dir / "ledger.db")
        try:
            chain = DurableChain(
                AgentKey.load_or_create(self.logs_dir / "agent.key"), store
            )
            gate = Gate(
                chain,
                benchmark_policy(),
                RemoteResolver(self._workspace, venue=venue.name),
                run_id=run_id,
            )
            self._open_envelope(gate, venue)

            tools = RemoteTools(
                gate=gate, venue=venue, workspace=self._workspace, actor="agent"
            )
            # The model is chosen before the window, because the window's budget
            # follows the routed model's declared context.
            llm = self._build_llm(model_name, gate)
            total, reserve = _context_budget(llm)
            window = ContextWindow(
                ContextBudget(total=total, reserve_output=reserve, keep_recent=6),
                count_tokens=token_counter_for(model_name or llm.model),
            )
            loop = AgentLoop(
                gate=gate, tools=tools, window=window, llm=llm,
                limits=self._limits(), run_id=run_id, stop=self._stop,
            )

            environment_note = probe_environment(tools)
            outcome = loop.run(instruction, environment=environment_note)
            gate.close_envelope("run finished")

            metrics = self._metrics_for(store, run_id, venue=venue)
        finally:
            store.close()

        trajectory = build_trajectory(
            outcome,
            instruction=instruction,
            model_name=model_name,
            session_id=self.session_id,
            extra=metrics,
        )
        return outcome, metrics, trajectory

    def _build_llm(self, model_name: str, gate: Gate):
        """Route locally when a manifest exists; fall back to one named model.

        Local-first is the default and reaching a hosted API takes two explicit
        steps — a manifest that declares the engine, and `OPTIMUS_ALLOW_REMOTE`.
        Neither can happen by omission, which is the whole point of the gate in
        `loop/engines.py`.
        """
        allow_remote = _truthy(self._get_env("OPTIMUS_ALLOW_REMOTE"))
        try:
            registry = Registry.load(self._get_env("OPTIMUS_ENGINES") or None)
        except ConfigError as exc:
            if not model_name:
                raise ValueError(
                    f"no engine manifest ({exc}) and no model: write "
                    "configs/engines.toml, or pass `harbor run -m ...`"
                ) from exc
            self.logger.info(f"no engine manifest ({exc}); using {model_name} directly")
            return LiteLLM(
                model_name=model_name,
                temperature=float(self._get_env("OPTIMUS_TEMPERATURE") or 0.0),
                max_output_tokens=int(self._get_env("OPTIMUS_MAX_OUTPUT") or 8_000),
            )

        # `-m` names a declared model id when it matches one, and otherwise is
        # taken as a literal provider string — so `harbor run -m qwen35-9b` pins
        # a local model and `-m gemini/...` still works.
        declared = {m.id for m in registry.models}
        if model_name and model_name not in declared:
            self.logger.info(
                f"{model_name} is not declared in {registry.source}; calling it directly"
            )
            return LiteLLM(
                model_name=model_name,
                temperature=float(self._get_env("OPTIMUS_TEMPERATURE") or 0.0),
                max_output_tokens=int(self._get_env("OPTIMUS_MAX_OUTPUT") or 8_000),
            )

        router = RoutedLLM(
            registry=registry,
            allow_remote=allow_remote,
            model_id=model_name,
            temperature=float(self._get_env("OPTIMUS_TEMPERATURE") or 0.0),
            record=lambda kind, payload: gate.chain.append(
                kind, {**payload, "run_id": gate.run_id}, TrustLabel.TRUSTED_LOCAL
            ),
        )
        candidates, excluded = registry.candidates(
            allow_remote=allow_remote, model_id=model_name
        )
        self.logger.info(
            f"routing over {[c.label for c in candidates]}"
            + (f"; excluded {excluded}" if excluded else "")
        )
        return router

    # -- authorisation --------------------------------------------------------

    def _open_envelope(self, gate: Gate, venue: RemoteVenue) -> None:
        """Load the owner-signed envelope, or run without one and say so.

        Deliberately not fatal. A trial with no envelope is a legitimate — and
        informative — row: it reports how many operator interventions this task
        would have required, which is a number the board has never seen.
        """
        path = self._get_env("OPTIMUS_ENVELOPE")
        fingerprint = self._get_env("OPTIMUS_OWNER_FINGERPRINT")
        if not path:
            self.logger.warning(
                "no OPTIMUS_ENVELOPE: every mutation will park for approval that "
                "nobody is present to give. Issue one with `optimus envelope`."
            )
            return
        try:
            envelope = Envelope.from_dict(
                json.loads(Path(path).read_text(encoding="utf-8"))
            )
            gate.open_envelope(envelope, owner_fingerprint=fingerprint or "")
        except (OSError, ValueError, KeyError, EnvelopeRefused) as exc:
            # Refused, not downgraded. The run continues with no envelope, which
            # is a different and clearly-labelled thing from running with one.
            self.logger.error(f"envelope refused, continuing without it: {exc}")
            return
        self._envelope_id = envelope.envelope_id
        self.logger.info(
            f"envelope {envelope.envelope_id} opened "
            f"(venue {venue.name} reports {venue.isolation().name} isolation)"
        )

    def _limits(self) -> LoopLimits:
        def _int(key: str, default: int) -> int:
            raw = self._get_env(key)
            return int(raw) if raw else default

        def _float(key: str, default: float) -> float:
            raw = self._get_env(key)
            return float(raw) if raw else default

        return LoopLimits(
            max_turns=_int("OPTIMUS_MAX_TURNS", 60),
            max_wall_s=_float("OPTIMUS_MAX_WALL_S", 1_800.0),
            max_cost_usd=_float("OPTIMUS_MAX_COST_USD", 0.0),
            observation_chars=_int("OPTIMUS_OBSERVATION_CHARS", 6_000),
            max_consecutive_denials=_int("OPTIMUS_MAX_DENIALS", 6),
            # Exposed because a local engine that is merely asleep wants a very
            # different wait from a hosted one that is rate-limiting, and
            # because a test should not have to spend six real minutes proving
            # the backoff works.
            max_transient_errors=_int("OPTIMUS_MAX_TRANSIENT_ERRORS", 8),
            retry_backoff_s=_float("OPTIMUS_RETRY_BACKOFF_S", 4.0),
            max_backoff_s=_float("OPTIMUS_MAX_BACKOFF_S", 120.0),
        )

    # -- what gets published --------------------------------------------------

    def _metrics_for(
        self,
        store: LedgerStore,
        run_id: str,
        *,
        venue: RemoteVenue | None = None,
    ) -> dict[str, Any]:
        """The receipt, folded out of the ledger.

        There is exactly one of these, used both when a run finishes normally
        and when `populate_context_post_run` rebuilds after a killed trial. Two
        builders is how `provider_errors` came to exist only on the crash path
        and `engine` only on the other — a published metric that appears when
        the run fails and vanishes when it succeeds is worse than no metric.

        Everything countable is read back out of the ledger rather than off the
        in-memory loop, so the published number and the signed record are the
        same number by construction. `solved` is absent on purpose: the verifier
        has not run yet, and `report.py` joins its verdict to this file.
        """
        events = store.events()
        meter = aggregate(events, run_id=run_id)
        finished = [e for e in events if e.kind == "run.finished"]
        opened = [e for e in events if e.kind == "envelope.opened"]
        routes = [e for e in events if e.kind == "model.route"]
        ending = finished[-1].payload if finished else {}

        metrics: dict[str, Any] = {
            "run_id": run_id,
            "harness": AGENT_NAME,
            "harness_version": VERSION,
            "model": self.model_name,
            # A run that never reached its own `run.finished` says so, rather
            # than borrowing a stop reason it never recorded.
            "stop_reason": ending.get("stop_reason", "killed_before_finishing"),
            "reconstructed_from_ledger": not finished,
            "turns": meter.turns,
            "input_tokens": meter.input_tokens,
            "output_tokens": meter.output_tokens,
            "cached_tokens": meter.cached_tokens,
            "total_tokens": meter.total_tokens,
            "cost_usd": round(meter.cost_usd, 6),
            # The three nobody publishes.
            "no_action_turns": meter.no_action_turns,
            "provider_errors": meter.provider_errors,
            "breakers_fired": meter.breakers_fired,
            # Refusals and interventions: what a Gate buys, as a number.
            "unsafe_attempts_refused": meter.denials,
            "operator_interventions_required": meter.approvals_required,
            "actions": meter.actions,
            "actions_settled_ok": meter.settled_ok,
            "compactions": sum(1 for e in events if e.kind == "context.compacted"),
            "wall_ms": ending.get("wall_ms", meter.wall_ms),
            "summary": ending.get("summary", ""),
            # Named, not described: the full document is already an
            # `envelope.opened` row in the ledger shipping alongside this file.
            "envelope": (
                opened[-1].payload.get("envelope_id") if opened else None
            ),
            "envelope_uses": sum(1 for e in events if e.kind == "envelope.used"),
            "workspace": self._workspace,
            "ledger_rows": len(events),
        }
        # Which model actually served the turns. On a local-first system this
        # belongs in every receipt, not only in the ones rebuilt after a crash.
        if routes:
            route = routes[-1].payload
            metrics["engine"] = route.get("engine")
            metrics["routed_model"] = route.get("model")
            metrics["local"] = route.get("local")
        if venue is not None:
            metrics["venue"] = venue.name
            metrics["isolation"] = venue.isolation().name
        return metrics


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _context_budget(llm: Any = None) -> tuple[int, int]:
    """(window, reserved for output), from the routed model's own declaration.

    A 9B served with `--ctx-size 32768` must not be handed a 128K budget: the
    compaction plane would never fire and the server would refuse instead —
    which is the same context loss with none of the accounting. The reserve
    follows the model's `max_output_tokens` rather than a fixed 8K, because
    holding back twice what a model can emit is throwing away context that was
    paid for.
    """
    total = 128_000
    reserve = 8_000
    registry = getattr(llm, "registry", None)
    if registry is not None:
        picked, _ = registry.candidates(
            allow_remote=llm.allow_remote, model_id=llm.model_id
        )
        if picked:
            total = picked[0].model.context_tokens
            reserve = picked[0].model.max_output_tokens
    raw = os.environ.get("OPTIMUS_CONTEXT_TOKENS")
    if raw:
        total = int(raw)
    # Never reserve more than a quarter of the window, and never so little that
    # a full answer cannot land.
    reserve = max(1_024, min(reserve, total // 4))
    return total, reserve

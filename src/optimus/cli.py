"""The operator surface.

Small on purpose. These are the operations that must be reachable by a human and
must *not* be reachable by the loop:

* `attest` is the only thing that touches an `OwnerKey`. It lives here, in a
  command a person runs, rather than anywhere the Gate can call — which is the
  whole difference between a receipt that proves provenance and Bellona's, which
  proved only that a program had been self-consistent (`audit.md` §2.3).
* `undo` replays inverses. It is a recovery action, so it answers to the
  operator, not to policy.

argparse rather than a CLI framework: this file should not add a dependency to a
package whose only runtime dependency is a crypto library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .explain import explain_trial, find_trials, render, render_job
from .gate.envelope import ANY_WORKSPACE, DEFAULT_VERBS
from .gate.envelope import issue as issue_envelope
from .ledger.chain import attest as sign_checkpoint
from .ledger.keys import AgentKey, OwnerKey, fingerprint
from .ledger.store import LedgerStore
from .loop.engines import ConfigError, Registry
from .meter import aggregate
from .report import report_for
from .reversal.blobs import BlobStore
from .reversal.compensator import Compensator, record_undo


def _store(args: argparse.Namespace) -> LedgerStore:
    path = Path(args.ledger)
    if not path.exists():
        print(f"no ledger at {path}", file=sys.stderr)
        raise SystemExit(2)
    return LedgerStore(path)


def cmd_keygen(args: argparse.Namespace) -> int:
    path = Path(args.out)
    if path.exists():
        print(f"refusing to overwrite {path}", file=sys.stderr)
        return 2
    key = OwnerKey.generate()
    key.save(path)
    print(f"owner key written to {path}")
    print(f"fingerprint: {key.fingerprint}")
    print()
    print("Record that fingerprint somewhere the machine cannot edit. Verification")
    print("without it proves only that a chain is internally consistent.")
    return 0


def cmd_attest(args: argparse.Namespace) -> int:
    owner = OwnerKey.load(args.owner)
    with _store(args) as store:
        events = store.events()
        if not events:
            print("nothing to attest", file=sys.stderr)
            return 2
        cp = sign_checkpoint(owner, events)
        store.put_checkpoint(cp)
        print(f"attested {len(events)} rows through seq {cp.head_seq}")
        print(f"owner fingerprint: {fingerprint(cp.owner_pub)}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    with _store(args) as store:
        report = store.verify(expected_owner_fingerprint=args.owner_fingerprint)
        print(report.render())
        for f in report.failures:
            print(f"  x {f}")
        # 0 valid, 1 unattested, 2 tampered — distinct because they mean
        # genuinely different things and a script should be able to tell.
        if report.fully_valid:
            return 0
        return 1 if report.chain_valid and report.signatures_valid else 2


def cmd_envelope(args: argparse.Namespace) -> int:
    """Mint a standing authorisation for an unattended run.

    Here for the same reason `attest` is: it needs the owner key, and the owner
    key must never be reachable from anything the Gate can call. An agent that
    could issue its own envelope would be approving its own actions, which is
    the whole failure this design exists to avoid (`gate/envelope.py`).
    """
    import json

    if bool(args.workspace) == bool(args.any_workspace):
        print(
            "give exactly one of --workspace PATH or --any-workspace",
            file=sys.stderr,
        )
        return 2

    owner = OwnerKey.load(args.owner)
    envelope = issue_envelope(
        owner,
        principal=args.principal,
        actor=args.actor,
        workspace=ANY_WORKSPACE if args.any_workspace else args.workspace,
        venues=tuple(args.venue),
        verbs=tuple(args.verb) if args.verb else DEFAULT_VERBS,
        max_actions=args.max_actions,
        ttl_ms=int(args.ttl_hours * 3600 * 1000),
        reason=args.reason,
        observed_isolation=args.isolation,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(envelope.to_dict(), indent=2), encoding="utf-8")
    print(f"envelope {envelope.envelope_id} written to {out}")
    print(f"  actor    {envelope.actor}")
    print(f"  verbs    {', '.join(envelope.verbs)}")
    print(f"  venues   {', '.join(envelope.venues)}")
    print(f"  where    {'any path in the venue' if envelope.venue_scoped else envelope.workspace}")
    print(f"  ceiling  {envelope.max_actions} actions, {args.ttl_hours}h")
    if envelope.venue_scoped:
        print()
        print("  NOTE: this envelope bounds by venue, not by path. Every filesystem")
        print("  target inside the named venue is in scope. Path containment still")
        print("  comes from the run's own resolver; this document adds no second")
        print("  check on it. Use --workspace when the path is known in advance.")
    print()
    print("Point a run at it with:")
    print(f"  OPTIMUS_ENVELOPE={out}")
    print(f"  OPTIMUS_OWNER_FINGERPRINT={fingerprint(envelope.owner_pub)}")
    print()
    print("Without the fingerprint the Gate refuses the envelope, because a")
    print("document checked against the key it carries proves nothing.")
    return 0


def cmd_engines(args: argparse.Namespace) -> int:
    """What can serve a turn, and what would be refused.

    Answers the question this layer gets asked most: "why did it not use the
    local one." Routing exclusions are printed with their reasons rather than
    left in a log.
    """
    from .loop.router import live_models

    try:
        registry = Registry.load(args.config)
    except ConfigError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    print(registry.describe())

    for label, allow_remote in (("local-only (the default)", False),
                                ("with --allow-remote", True)):
        candidates, excluded = registry.candidates(allow_remote=allow_remote)
        print(f"\nroute order, {label}:")
        for i, candidate in enumerate(candidates, 1):
            print(f"  {i}. {candidate.label}")
        if not candidates:
            print("  (nothing routable)")
        for reason in excluded:
            print(f"  x {reason}")

    if args.live:
        print("\nwhat each engine is actually serving:")
        found = live_models(registry)
        if not found:
            print("  (no engine answered)")
        declared = {m.id for m in registry.models}
        for engine_id, models in found.items():
            for model in models:
                mark = " " if model in declared else "*"
                print(f"  {mark} {engine_id}: {model}")
        undeclared = {m for ms in found.values() for m in ms} - declared
        if undeclared:
            print(f"\n  * {len(undeclared)} model(s) served but not declared. This file is")
            print("    never rewritten automatically: a discovered model has no declared")
            print("    context size or tool support to route on. Add them deliberately.")
    return 0


def cmd_acp(args: argparse.Namespace) -> int:
    """Serve the Agent Client Protocol on stdio, for an editor to launch.

    This is the interactive counterpart to a benchmark run, and the difference
    that matters is authorisation. Under Harbor there is no human, so the
    autonomy envelope clears the untrusted-mutation invariant in advance. Here
    there *is* a human sitting in an editor, and ACP has a round trip to ask
    them — so the default is **no envelope at all**: the Gate parks, the editor
    asks, and the assent names what the person was actually shown. That is
    strictly stronger than agreeing to a scope in advance.

    `--envelope` is still accepted, for an editor session meant to run
    unattended within a bounded scope. It is not the default, because "there is
    nobody there" should need an explicit argument rather than be inherited from
    the benchmark path.
    """
    from .context.window import ContextBudget, ContextWindow
    from .gate.gate import Gate
    from .gate.policy import baseline_policy
    from .gate.resolvers import WorkspaceResolver
    from .ledger.store import DurableChain, LedgerStore
    from .loop.agent import AgentLoop, LoopLimits
    from .loop.llm import token_counter_for
    from .loop.router import RoutedLLM
    from .surface.acp import ACPServer
    from .tools.std import GatedTools
    from .venues.local import LocalVenue

    workspace = os.path.abspath(args.workspace)
    if not os.path.isdir(workspace):
        print(f"no such workspace: {workspace}", file=sys.stderr)
        return 2
    if args.envelope and not args.owner_fingerprint:
        # The fingerprint has to arrive from somewhere the document does not
        # control. Reading it back out of the envelope would make the check
        # circular, which is the whole failure `verify()` exists to prevent.
        print(
            "--envelope needs --owner-fingerprint, and it must come from your "
            "own key rather than from the envelope file",
            file=sys.stderr,
        )
        return 2

    try:
        registry = Registry.load(args.config)
    except ConfigError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    state_dir = Path(args.state)
    state_dir.mkdir(parents=True, exist_ok=True)
    store = LedgerStore(state_dir / "acp-ledger.db")

    def run_factory(session_id: str, text: str, bus, control):
        def run():
            gate = Gate(
                DurableChain(
                    AgentKey.load_or_create(state_dir / "agent.key"), store
                ),
                # Not `benchmark_policy()`. That one has no approval rules at
                # all, because under Harbor there is nobody to approve. Here
                # `baseline_policy` stages writes and approves execution, and
                # every one of those parked tickets is a question ACP can put
                # to the person in the editor.
                baseline_policy(),
                WorkspaceResolver(workspace),
                run_id=session_id,
            )
            if args.envelope:
                _open_envelope_file(gate, args.envelope, args.owner_fingerprint)
            llm = RoutedLLM(registry, allow_remote=args.allow_remote)
            window = ContextWindow(
                ContextBudget(
                    total=args.context, reserve_output=2_048, keep_recent=6
                ),
                count_tokens=token_counter_for(llm.model),
            )
            return AgentLoop(
                gate=gate,
                tools=GatedTools(gate=gate, venues=[LocalVenue()]),
                window=window,
                llm=llm,
                limits=LoopLimits(max_turns=args.max_turns),
                run_id=session_id,
                bus=bus,
                control=control,
            ).run(text)
        return run

    try:
        ACPServer(run_factory=run_factory).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


def _open_envelope_file(gate, path: str, owner_fingerprint: str) -> None:
    from .gate.envelope import Envelope

    envelope = Envelope.from_dict(json.loads(Path(path).read_text()))
    gate.open_envelope(envelope, owner_fingerprint=owner_fingerprint)


def cmd_why(args: argparse.Namespace) -> int:
    """Explain a trial, or a whole job, from its ledger.

    Every finding in STATUS.md was reached by writing a one-off script against
    a ledger. This is that script, kept.
    """
    trials = find_trials(args.target)
    if not trials:
        print(f"no trial ledger under {args.target}", file=sys.stderr)
        return 2

    if len(trials) > 1 and not args.each:
        rendered = []
        for name, path in trials:
            explanation = explain_trial(path)
            if explanation is not None:
                rendered.append((name, explanation))
        print(render_job(rendered))
        print(f"\n{len(rendered)} trial(s). Add --each for the turn-by-turn account.")
        return 0

    for i, (name, path) in enumerate(trials):
        explanation = explain_trial(path)
        if explanation is None:
            continue
        if i:
            print("\n" + "=" * 72 + "\n")
        if len(trials) > 1:
            print(f"{name}\n")
        print(render(explanation, timeline=not args.no_timeline))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """pass^k and cost, joined from a Harbor run directory."""
    import json

    report = report_for(args.run_dir)
    if not report.trials:
        print(f"no trial results under {args.run_dir}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.render())
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    comp = Compensator(BlobStore(Path(args.state) / "blobs"))
    with _store(args) as store:
        events = store.events()
        report = comp.undo(events, since_seq=args.since)
        for line in report.applied:
            print(f"  undone: {line}")
        for line in report.skipped:
            print(f"  skipped: {line}")
        for line in report.failed:
            print(f"  FAILED: {line}", file=sys.stderr)
        print(report.render())
        if report.applied or report.failed:
            chain = _resumable_chain(args, store)
            record_undo(chain, report, run=args.run)
    return 0 if report.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    with _store(args) as store:
        events = store.events()
        meter = aggregate(events)
        kinds: dict[str, int] = {}
        for e in events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        print(f"ledger: {len(events)} rows, {len(store.checkpoints())} checkpoint(s)")
        for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>6}  {kind}")
        print(meter.render())
    return 0


def _resumable_chain(args: argparse.Namespace, store: LedgerStore):
    from .ledger.store import DurableChain

    return DurableChain(AgentKey.load_or_create(Path(args.state) / "agent.key"), store)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="optimus", description="Optimus operator commands")
    sub = p.add_subparsers(dest="command", required=True)

    def with_ledger(sp):
        sp.add_argument("--ledger", default="state/ledger.db", help="path to the ledger database")
        return sp

    kg = sub.add_parser("keygen", help="generate an owner key (do this once, off the agent's path)")
    kg.add_argument("--out", default="state/owner.key")
    kg.set_defaults(func=cmd_keygen)

    at = with_ledger(sub.add_parser("attest", help="sign a checkpoint over the ledger"))
    at.add_argument("--owner", default="state/owner.key")
    at.set_defaults(func=cmd_attest)

    ve = with_ledger(sub.add_parser("verify", help="verify the ledger against a known owner"))
    ve.add_argument("--owner-fingerprint", required=True,
                    help="required: verifying against the keys a chain carries proves nothing")
    ve.set_defaults(func=cmd_verify)

    un = with_ledger(sub.add_parser("undo", help="replay recorded inverses, newest first"))
    un.add_argument("--state", default="state")
    un.add_argument("--since", type=int, default=0)
    un.add_argument("--run", default="")
    un.set_defaults(func=cmd_undo)

    st = with_ledger(sub.add_parser("status", help="what the ledger contains, and what it cost"))
    st.set_defaults(func=cmd_status)

    en = sub.add_parser(
        "envelope",
        help="sign a standing authorisation so an unattended run can act",
    )
    en.add_argument("--owner", default="state/owner.key")
    en.add_argument("--out", default="state/envelope.json")
    en.add_argument("--principal", required=True,
                    help="who is authorising this; recorded in the ledger")
    en.add_argument("--actor", default="agent")
    en.add_argument("--workspace", default="",
                    help="the one workspace root it covers, as the run will see it")
    en.add_argument("--any-workspace", action="store_true",
                    help="bound by venue instead of by path. Needed for a benchmark "
                         "suite, where each task's workdir comes from its own image "
                         "and is not known until the container runs. Widens the "
                         "envelope: read the note it prints.")
    en.add_argument("--venue", action="append", default=[],
                    help="venue name it is valid in; repeatable (e.g. --venue harbor)")
    en.add_argument("--verb", action="append", default=[],
                    help=f"verb it clears; repeatable. Default: {', '.join(DEFAULT_VERBS)}")
    en.add_argument("--max-actions", type=int, default=500)
    en.add_argument("--ttl-hours", type=float, default=6.0)
    en.add_argument("--isolation", default="",
                    help="isolation the venue was observed to report; recorded as evidence")
    en.add_argument("--reason", default="")
    en.set_defaults(func=cmd_envelope)

    en2 = sub.add_parser(
        "engines", help="what can serve a turn, local first, and what is refused"
    )
    en2.add_argument("--config", default=None,
                     help="engine manifest; defaults to configs/engines.toml")
    en2.add_argument("--live", action="store_true",
                     help="also ask each engine what it is really serving")
    en2.set_defaults(func=cmd_engines)

    wy = sub.add_parser(
        "why", help="explain a trial or a job from its ledger: where the turns "
                    "and tokens went, and what stopped it"
    )
    wy.add_argument("target", help="a trial directory, a job directory, or a ledger")
    wy.add_argument("--each", action="store_true",
                    help="turn-by-turn for every trial, not a one-line summary each")
    wy.add_argument("--no-timeline", action="store_true",
                    help="skip the per-turn prompt-size chart")
    wy.set_defaults(func=cmd_why)

    ac = sub.add_parser(
        "acp", help="serve the Agent Client Protocol on stdio, so an editor "
                    "(Zed, JetBrains) can drive this agent"
    )
    ac.add_argument("--workspace", default=".",
                    help="the root the agent may act in; everything outside is refused")
    ac.add_argument("--state", default="state",
                    help="where the session ledger and agent key live")
    ac.add_argument("--config", default="configs/engines.toml")
    ac.add_argument("--allow-remote", action="store_true",
                    help="permit hosted engines. Off by default; local-first.")
    ac.add_argument("--context", type=int, default=32_768)
    ac.add_argument("--max-turns", type=int, default=40)
    ac.add_argument("--envelope", default="",
                    help="run unattended within a signed scope. Off by default: "
                         "an editor has a human in it, and ACP can ask them.")
    ac.add_argument("--owner-fingerprint", default="",
                    help="required with --envelope, and it must come from your "
                         "own key rather than from the envelope file")
    ac.set_defaults(func=cmd_acp)

    rp = sub.add_parser(
        "report", help="pass^k, tokens per solved task and refusals from a Harbor run"
    )
    rp.add_argument("run_dir", help="a Harbor run directory containing trial results")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

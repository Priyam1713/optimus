"""M3 end to end, on this machine, in one process.

`STATUS.md` finding M2-1 is the reason this file exists: a 115-test suite passed
while the harness silently lowercased every filename it created, and running the
thing for real is what caught it. So every milestone gets a demo that exercises
the whole path rather than each plane separately.

What this runs:

    optimus keygen  ->  optimus envelope  ->  the loop, in a real shell
                    ->  optimus attest    ->  optimus verify  ->  optimus report

with a scripted model by default, so it needs no API key and no network. Point
`--model` at a real model and it runs the same path against that instead.

The shell is a genuine `bash` in a temporary POSIX workspace, reached through the
same `RemoteVenue` the Harbor adapter uses — so the transport, the remote
resolver, the base64 file channel and the Gate are all doing real work. What it
is *not* is a container: `--isolation` is reported honestly as PROCESS here, and
`choose()` would refuse a CONTAINER request. That difference is the point of
declaring isolation rather than assuming it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from optimus.context.window import ContextBudget, ContextWindow
from optimus.gate.envelope import Envelope, issue
from optimus.gate.gate import Gate
from optimus.gate.policy import benchmark_policy
from optimus.gate.remote import RemoteResolver
from optimus.ledger.chain import attest
from optimus.ledger.keys import AgentKey, OwnerKey
from optimus.ledger.store import DurableChain, LedgerStore
from optimus.loop.agent import AgentLoop, LoopLimits
from optimus.loop.llm import ModelReply, ScriptedLLM, ToolCall, Usage
from optimus.meter import aggregate
from optimus.report import Report, Trial
from optimus.tools.remote import RemoteTools, probe_environment
from optimus.venues.base import Isolation
from optimus.venues.remote import RemoteExec, RemoteVenue

TASK = (
    "Create a file `report/summary.txt` in the workspace containing the exact "
    "line `CHANGELOG.md ok`, then verify it reads back correctly."
)


class LocalShell:
    """A real bash, at an honestly-declared isolation level.

    `cd` happens inside the shell because the workspace is a POSIX path this
    host cannot chdir into — the same shape as a container, minus the walls.
    """

    def __init__(self, bash: str):
        self.bash = bash

    def __call__(self, argv, *, cwd, timeout_s):
        argv = list(argv)
        script = (
            argv[2] if len(argv) == 3 and argv[1] in ("-lc", "-c")
            else shlex.join(argv)
        )
        proc = subprocess.run(
            [self.bash, "-lc", f"cd {shlex.quote(cwd)} && {script}"],
            capture_output=True, text=True, timeout=timeout_s + 10,
            env={**os.environ},
        )
        return RemoteExec(
            exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )


def scripted_model() -> ScriptedLLM:
    """A plausible trajectory, including the failures worth exercising.

    It deliberately contains an idle turn, a refused action and a repeat, so the
    demo prints numbers that are not all zero — a receipt full of zeroes tells
    you nothing about whether the counting works.
    """
    usage = Usage(input_tokens=1_800, output_tokens=90, cached_tokens=1_200,
                  cost_usd=0.0042)

    def reply(*calls, text=""):
        return ModelReply(
            text=text,
            tool_calls=tuple(
                ToolCall(call_id=f"c{uuid.uuid4().hex[:8]}", name=n, arguments=a)
                for n, a in calls
            ),
            usage=usage,
        )

    return ScriptedLLM([
        reply(("bash", {"command": "ls -1A"}), text="Looking at the workspace."),
        # An action the Gate must refuse: outside the workspace entirely.
        reply(("write_file", {"path": "../../etc/evil", "content": "x"}),
              text="Trying somewhere I should not be able to reach."),
        # A turn that does nothing, so the breaker has something to break.
        reply(text="Let me think about that refusal."),
        reply(("write_file", {"path": "report/summary.txt",
                              "content": "CHANGELOG.md ok\n"}),
              text="Writing the file."),
        reply(("bash", {"command": "cat report/summary.txt"}),
              text="Reading it back."),
        reply(("bash", {"command": "cat report/summary.txt"}),
              text="Reading it back again."),
        reply(("finish", {"summary": "Wrote report/summary.txt and verified it."}),
              text="Done."),
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="",
                        help="a litellm model id; omit to use the scripted model")
    parser.add_argument("--state", default="", help="where to put keys and ledger")
    parser.add_argument("--keep", action="store_true", help="keep the workspace")
    args = parser.parse_args()

    bash = shutil.which("bash")
    if bash is None:
        print("this demo needs a POSIX shell on PATH", file=sys.stderr)
        return 2

    state = Path(args.state or tempfile.mkdtemp(prefix="optimus-m3-"))
    state.mkdir(parents=True, exist_ok=True)
    workspace = f"/tmp/optimus-demo-{uuid.uuid4().hex[:8]}"
    subprocess.run([bash, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)
    print(f"state      {state}")
    print(f"workspace  {workspace}")

    try:
        return _run(bash, state, workspace, args.model)
    finally:
        if not args.keep:
            subprocess.run(
                [bash, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )


def _run(bash: str, state: Path, workspace: str, model: str) -> int:
    # -- 1. the owner key, off the agent's path -----------------------------
    owner_path = state / "owner.key"
    owner = OwnerKey.generate()
    owner.save(owner_path)
    print(f"\n[1] owner key      fingerprint {owner.fingerprint}")

    venue = RemoteVenue(LocalShell(bash), name="demo-shell",
                        isolation=Isolation.PROCESS)

    # -- 2. the envelope, signed by that key --------------------------------
    envelope = issue(
        owner,
        principal="demo-operator",
        actor="agent",
        workspace=workspace,
        venues=(venue.name,),
        max_actions=50,
        reason="m3 demo",
        observed_isolation=venue.isolation().name,
    )
    (state / "envelope.json").write_text(
        json.dumps(envelope.to_dict(), indent=2), encoding="utf-8"
    )
    print(f"[2] envelope       {envelope.envelope_id}, "
          f"{envelope.max_actions} actions, venue {venue.name}")

    # -- 3. the run ----------------------------------------------------------
    run_id = "demo-run"
    store = LedgerStore(state / "ledger.db")
    try:
        gate = Gate(
            DurableChain(AgentKey.load_or_create(state / "agent.key"), store),
            benchmark_policy(),
            RemoteResolver(workspace, venue=venue.name),
            run_id=run_id,
        )
        gate.open_envelope(
            Envelope.from_dict(json.loads((state / "envelope.json").read_text())),
            owner_fingerprint=owner.fingerprint,
        )

        tools = RemoteTools(gate=gate, venue=venue, workspace=workspace)
        if model:
            from optimus.loop.llm import LiteLLM, token_counter_for

            llm = LiteLLM(model_name=model)
            counter = token_counter_for(model)
        else:
            from optimus.context.episodes import heuristic_tokens

            llm, counter = scripted_model(), heuristic_tokens

        loop = AgentLoop(
            gate=gate, tools=tools,
            window=ContextWindow(ContextBudget(total=32_000), count_tokens=counter),
            llm=llm, limits=LoopLimits(max_turns=20), run_id=run_id,
        )
        primer = probe_environment(tools)
        print(f"[3] priming        {len(primer)} chars of structure, one round trip")
        outcome = loop.run(TASK, environment=primer)
        gate.close_envelope("demo finished")
        print(f"    {outcome.render()}")

        # -- 4. did the effect actually land? -------------------------------
        check = subprocess.run(
            [bash, "-lc", f"cat {shlex.quote(workspace)}/report/summary.txt"],
            capture_output=True, text=True,
        )
        solved = check.stdout.strip() == "CHANGELOG.md ok"
        print(f"[4] verifier       file says {check.stdout.strip()!r} -> "
              f"solved={solved}")

        # -- 5. attest and verify -------------------------------------------
        events = store.events()
        store.put_checkpoint(attest(owner, events))
        report = store.verify(expected_owner_fingerprint=owner.fingerprint)
        print(f"[5] ledger         {len(events)} rows; {report.render()}")

        meter = aggregate(events, run_id=run_id, solved=solved)
        print(f"    {meter.render()}")
        kinds: dict[str, int] = {}
        for event in events:
            kinds[event.kind] = kinds.get(event.kind, 0) + 1
        print("    " + "  ".join(f"{k}={n}" for k, n in sorted(kinds.items())))
    finally:
        store.close()

    # -- 6. the published row ------------------------------------------------
    trial = Trial(
        task="demo", trial_dir=str(state), solved=solved,
        reward=1.0 if solved else 0.0,
        metrics={
            "total_tokens": meter.total_tokens,
            "input_tokens": meter.input_tokens,
            "cached_tokens": meter.cached_tokens,
            "no_action_turns": meter.no_action_turns,
            "unsafe_attempts_refused": meter.denials,
            "operator_interventions_required": meter.approvals_required,
            "cost_usd": meter.cost_usd,
        },
    )
    print("\n[6] the row that goes on the board")
    print(Report([trial]).render())

    ok = solved and report.fully_valid
    print(f"\n{'PASS' if ok else 'FAIL'}: effect landed={solved} "
          f"ledger valid={report.fully_valid}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

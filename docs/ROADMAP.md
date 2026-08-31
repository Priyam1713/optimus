# Roadmap

Where this goes next, in the order the evidence says to do it. Milestone
definitions come from [apex.md](apex.md) §7; what follows is the plan as it
stands *after* ten real Terminal-Bench runs, which changed several priorities.

Current position: **M0–M3 built, 262 tests, one complete Terminal-Bench trial,
zero solved tasks.** See [STATUS.md](../STATUS.md).

---

## Now: the 10-task run

In flight. `qwen35-9b` locally, 10 tasks serial, ~20 min each.

It answers one question — **was the complete 40-turn trial representative or
lucky?** Specifically:

- Does the anchored context budget hold across tasks with different output
  shapes? Compaction fired 3 times on `gpt2-codegolf`; a task that cats large
  files will push it much harder.
- Do any tasks trip the `blocked` or `looping` breakers, and are those
  judgements right?
- What is the spread in turns-to-give-up? A model that burns 40 turns on every
  task is a different problem from one that finishes some in 12.

Deliverable: the first `optimus report` output over more than one task —
pass@k, pass^k, no-action turns per task, refusals, interventions.

**It will almost certainly report `solved=0`.** A 9B model on Terminal-Bench 2.0
is not expected to score; the tasks are hard enough that frontier models are
well under 50%. That is fine. This run is measuring the *harness*, and a suite
that runs to completion 10 times without a harness defect is the result being
bought.

---

## Next: make the failures legible (M3.5)

Ten runs produced ten failures and diagnosing each took a manual ledger query.
That does not scale to 89 tasks × 5 attempts.

1. ~~**`optimus why <trial-dir>`**~~ — **done.** Reads a trial or a whole job
   from its ledger: where the turns went, the prompt-size curve with compaction
   points marked, what the Gate refused, and what stopped it. Building it found
   three of its own bugs, one of which was the same class as the four it exists
   to diagnose: two renderers disagreeing about whether the environment probe
   counts as a turn, so the same run reported 40 in one view and 41 in another.

2. ~~**Per-turn context telemetry in the ledger.**~~ — **done.** A
   `context.turn` row now records, every turn, what the plane believed the next
   request would cost, the uncorrected estimate, the calibration, what the
   provider charged for the last one, and the allowance. `optimus why` reports
   the worst under-estimate and warns when the plane believed a request was
   smaller than it was — which is the shape of every context failure this
   project has had. The four bugs were only ever *one* run apart from being
   obvious; they needed the estimate and the bill in the same row.

3. **Trajectory diffing across attempts.** With `-k 5`, five attempts at the
   same task produce five trajectories. Where they diverge is where the model
   is unreliable, and that is what `pass^k` measures numerically without saying
   where.

## Then: the token ceiling in CI (M4, and the debt from apex §4)

[apex.md](apex.md) §4 sets the target — *within 2× of Goose's 28–37K tokens per
solved task* — and says a regression should fail the build like a test.

**This is now unblocked.** The ten-task run solved three, so
`tokens_per_solved_task` is a real number: **1,297,286**, against a ceiling of
roughly 74K. Missed by about 18×.

A first attempt at biting into it — telling the model how many turns it has —
produced one task at 430,650 tokens → 109,348, still solved, and one task that
ignored the change entirely and lost its solve. One trial each, so the two
results cancel rather than average. See
[STATUS.md § the turn budget](../STATUS.md#the-turn-budget), including the part
where the elaborate half of the change did nothing and the one-line half did all
of the good result.

What remains before a ceiling can go in CI:

- **Re-measure after the turn-budget change**, over more than one attempt per
  task. One trial is a signal, not a baseline, and a ceiling set from a lucky
  sample fails the build for the wrong reason.
- **Decide what the ceiling is a ceiling on.** `tokens_per_solved_task` over a
  10-task subset moves sharply with *which* tasks get solved, so the metric
  needs pinning to a fixed task set before it can gate anything.

Until then the honest CI gate is the one that exists: tests, lint, and the
end-to-end demo.

## ~~Also M4: surfaces~~ — largely done

See [STATUS.md § M4](../STATUS.md#m4--surfaces-partial). The event bus, the
steering plane, the ACP agent, the AG-UI emitter, REST+SSE, a terminal view and
the pre-flight dry-run are built, with 381 tests.

Both items the runs had promoted from "nice" to "needed" are closed:

- ~~**Per-turn `AgentContext` streaming.**~~ The loop publishes every turn as it
  happens, mirrored from the single point that writes the ledger so that a live
  view and `optimus why` cannot disagree about what happened.
- ~~**A cancellation path that does not depend on the adapter.**~~ `Control`
  owns the `threading.Event` the loop already polls. A TUI, an HTTP client and
  an editor's stop button all set the same bit; the Harbor adapter is unchanged.

Still open, deliberately:

- **WebSocket.** SSE is what shipped, and the documents say SSE. Revisit only if
  a client turns up that genuinely cannot use it — the cost is a web-framework
  dependency in a project whose entire runtime dependency is `cryptography`.
- **ACP v2.** v1 is implemented and a v2 client is negotiated down honestly. v2
  moves turn completion out of `session/prompt`, which is a real restructuring
  of the adapter rather than a field rename.
- **A web frontend.** Any AG-UI client can already consume the stream.

## Deferred, with the reason

- **The routing scorer.** Achilles ranks candidates on quality priors, latency,
  reliability and VRAM fit. Not ported: there is one capability and three local
  models, and Achilles's own docstring records that 84 of its 89 capabilities
  have a single eligible candidate anyway. It arrives when there is benchmark
  data to feed it — which the 10-task run starts producing.
- **A GPU arbiter.** llama.cpp's router already does residency and VRAM fitting.
  A second arbiter above one that works is two things to disagree.
- **Cross-run remote budget ledger.** Only matters if hosted engines become
  routine, and local-first says they should not.

## Unchanged from apex.md

**M5** skills and graduation · **M6** the OS/desktop plane
([architecture.md](architecture.md)) · **M7** multiplayer, durability, OS-level
policy enforcement, and the `NtCreateFile` work that closes the Windows TOCTOU
window.

---

## The standing risk

Four consecutive context bugs shipped past a green suite because each component
was correct in its own units. The mitigation is not more unit tests — it is that
**every milestone gets an end-to-end run against something real**, and that
`STATUS.md` leads with what does not work. Both are cheap. Neither is optional.

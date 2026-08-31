# Roadmap

Where this goes next, in the order the evidence says to do it. Milestone
definitions come from [apex.md](apex.md) §7; what follows is the plan as it
stands *after* ten real Terminal-Bench runs, which changed several priorities.

Current position: **M0–M3 built, 239 tests, one complete Terminal-Bench trial,
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

1. **`optimus why <trial-dir>`** — read a trial's ledger and print what
   happened: where turns went, what the breakers saw, what the Gate refused,
   where the context stood each turn. Every one of the M3-13..16 diagnoses in
   STATUS came from an ad-hoc script; that script should be a command.

2. **Per-turn context telemetry in the ledger.** `context.compacted` records
   the rendered size only when compaction runs. Recording it *every* turn makes
   the growth curve visible without inference, and would have caught the four
   context bugs in one run rather than four.

3. **Trajectory diffing across attempts.** With `-k 5`, five attempts at the
   same task produce five trajectories. Where they diverge is where the model
   is unreliable, and that is what `pass^k` measures numerically without saying
   where.

## Then: the token ceiling in CI (M4, and the debt from apex §4)

[apex.md](apex.md) §4 sets the target — *within 2× of Goose's 28–37K tokens per
solved task* — and says a regression should fail the build like a test. That
cannot be written yet, because **nothing has been solved and there is no
baseline to regress against.**

Two things unblock it:

- A model that can actually solve some tasks. `qwen38-27b` is already declared
  in the manifest and unloaded; it is the obvious next rung. A hosted model with
  billing enabled is the other, and `--allow-remote` already exists for it.
- Once any task is solved, `tokens_per_solved_task` becomes a real number and
  the ceiling can be set from it.

Until then the honest CI gate is the one that exists: tests, lint, and the
end-to-end demo.

## Also M4: surfaces

TUI and web over the REST/WS core, priority-queue steering, confidence
signalling, pre-flight dry-run, ACP client, AG-UI emitter. Plus two items the
runs promoted from "nice" to "needed":

- **Per-turn `AgentContext` streaming.** Currently populated at run end; a
  killed trial is rebuilt from the ledger afterwards, which works but means
  Harbor's live view shows nothing until the trial is over.
- **A cancellation path that does not depend on the adapter.** The loop takes a
  stop Event now, but only the Harbor adapter sets it.

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

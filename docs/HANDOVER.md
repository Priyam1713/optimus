# Handover

Written 2026-08-31 to continue this work in a fresh session. Delete it once the
next milestone lands — a handover that outlives its handover is just stale
documentation.

---

## What this is

**Optimus**, at `E:\optimus`, pushed to
<https://github.com/Priyam1713/optimus> (branch `main`).

An agent harness where every effect is authorized once through a Gate, recorded
once in a signed hash-chained ledger, and metered. Local-first: it drives
llama.cpp and cannot reach a hosted API unless a caller explicitly opts in.

Read in this order: [README.md](../README.md) → [STATUS.md](../STATUS.md) →
[docs/apex.md](apex.md) → [docs/ROADMAP.md](ROADMAP.md).

**[STATUS.md](../STATUS.md) is the source of truth.** It leads with what does
not work and carries 18 numbered findings from building this. Do not trust this
handover over it.

## Where things stand

- **M0–M3 built.** Gate, ledger, context plane, compensation, venues, the loop,
  the local-first model router, the Harbor adapter, `optimus why`.
- **262 tests, 2 skipped. Ruff clean.** `scripts/m3_demo.py` runs the whole path
  end to end and passes.
- **One complete Terminal-Bench trial**: 40 turns, 32 of 40 gated actions
  settled, 3 compactions, ledger verifies `VALID`. **Reward 0.0** — the model
  did not solve it.
- **Zero tasks solved, and no benchmark row exists.** That is the honest
  headline and it should stay at the top of STATUS.md until it changes.

## The immediate task

**Re-run 10 Terminal-Bench tasks locally.** The first attempt lost 8 of 10
trials to a transient DNS failure pulling container images (finding M3-18); the
retry defence is now in place.

State when this was written:

- **All 10 images pre-pulled and cached.** Verify with
  `docker images | grep alexgshaw` — expect 10. One (`write-compressor`) failed
  on the first attempt and succeeded on a retry, which is the same transient
  registry flakiness that cost the first run 8 trials.
- Envelope `env_01efcf8ad974c35c` has ~19 hours left. **Reissue it if expired**
  — an expired envelope refuses every action politely and wastes the whole run
  (finding M3-9, now caught at admission and in preflight).

```bash
# only if the envelope has expired or is under 30 min
.venv/Scripts/python.exe -m optimus.cli envelope --owner state/owner.key \
  --out state/envelope.json --principal "priya" --any-workspace \
  --venue harbor --max-actions 2000 --ttl-hours 24 --isolation CONTAINER

.venv/Scripts/python.exe scripts/bench.py --tasks 10 --concurrent 1 \
  --job-name optimus-10x --max-turns 40 --timeout-multiplier 4 --max-wall-s 2700
```

~20–25 min per task, so **4+ hours**. Run it in the background.

Then:

```bash
.venv/Scripts/python.exe -m optimus.cli why    jobs/optimus-10x   # per-trial table
.venv/Scripts/python.exe -m optimus.cli report jobs/optimus-10x   # pass^k and cost
```

**Expect `solved=0`.** A 9B on Terminal-Bench 2.0 is not expected to score;
frontier models are well under 50%. The run is measuring the *harness*. What it
buys is ten trials completing without a harness defect, and the first
`optimus report` over more than one task.

What to look for in the results:

- Does the anchored context budget hold on tasks with different output shapes?
  Compaction fired 3× on `gpt2-codegolf`; a task that cats large files will push
  harder.
- Any `blocked` or `looping` breakers, and were those judgements right?
- Spread in turns-to-give-up. 40 turns every time is a different problem from
  finishing some in 12.

## Environment

- **Windows 11.** The user's own shell is **Windows PowerShell 5.1** — no `&&`,
  no `mkdir -p`, no `printf`. Any command written *for them to run* must be
  PowerShell, with `Set-Content -Encoding ascii` for file writes (the default is
  UTF-16LE and silently corrupts config files). The Bash tool is separate Git
  Bash where POSIX is correct.
- **Python**: `.venv/Scripts/python.exe`. Installed with `-e ".[loop,harbor]"`.
- **llama.cpp router on `http://127.0.0.1:18080/v1`**, five models, all
  `--jinja` so tool calling works. `qwen35-9b` is the default route (32K ctx).
  Verify with `optimus engines --live`.
- **Docker** works; 20 CPUs, 27 GB. All 89 Terminal-Bench tasks cached under
  `~/.cache/harbor/tasks`.
- **No API key, and none needed.** A hosted Gemini engine is declared in
  `configs/engines.toml` and is excluded from every route unless `--allow-remote`
  is passed *and* `GEMINI_API_KEY` is set. The user's key file lives at
  `E:\Test\state\harbor.env` — **do not read it**; hand its path to
  `--env-file`. Its free tier allows 20 requests, which is fewer than one task
  needs.
- Reference implementation for the model layer: **Achilles** at
  `E:\Test\achilles-work` (`src/sovereign_ai/`). Harvest ideas; do not repair it.

## How this project works

Six rules, each of which cost a real failure. [CONTRIBUTING.md](../CONTRIBUTING.md)
has them in full.

1. **A plane that is correct in its own units is not correct.** Four separate
   context bugs (M3-13..16) shipped past a green suite because each component
   measured faithfully in a unit nothing downstream was billed in. This is the
   single most useful thing learned here.
2. **Where a provider knows a number, that number is truth and ours is a prior.**
3. **Tests are necessary and not sufficient.** 239 tests were green while four
   context bugs shipped. Ten real runs found them.
4. **Refuse rather than downgrade.**
5. **Never name a weak guarantee after a strong one.**
6. **The receipt is a projection, never a copy.**

## Working preferences

- **Be concise, and say a caution once.** The user asked for terse output and
  said "don't lecture me". Lead with the result.
- **Critique without deference.** They asked for unsparing assessment of systems
  they built themselves: "I don't want you to go easy on these two just because
  I built it."
- **Mount, don't rebuild**, and choose components as a global optimum under
  compatibility constraints rather than best-per-row.
- **Local-first, open-source-first.** Paid APIs are a later, opt-in integration.

## Traps this session actually hit

- **Heredocs in the Bash tool mangle `\n` inside Python string literals.** Use
  the Write/Edit tools for anything containing escapes.
- **`str.replace` without a count replaces every occurrence.** This broke three
  tests at once. Prefer Edit, or pass a count.
- **`--timeout-multiplier` is mandatory for local models.** Terminal-Bench allows
  900–1200s per task; `qwen35-9b` needs ~15 min for 30 turns. Without it a run
  is cut off on wall clock rather than capability, which measures the GPU.
- **Keep `--max-wall-s` under Harbor's agent timeout** so the loop stops itself
  and writes a receipt rather than being killed mid-turn.
- **Editing `src/` mid-run is safe** — Harbor runs trials in-process and Python
  caches modules — but it makes a run's ledgers inconsistent. Prefer to wait.

## Next, after the run

From [docs/ROADMAP.md](ROADMAP.md), in order:

1. **Trajectory diffing across attempts** — needs `-k > 1` to validate against,
   so it is unblocked by any multi-attempt run.
2. **A model that can actually solve something.** `qwen38-27b` is declared and
   unloaded; it is the obvious next rung. Until *something* is solved,
   `tokens_per_solved_task` is `inf` and the CI token ceiling from apex §4
   cannot be written, because there is no baseline to regress against.
3. **M4 surfaces**, plus per-turn `AgentContext` streaming and a cancellation
   path that does not depend on the Harbor adapter.

Deferred deliberately, with reasons in ROADMAP.md: the routing scorer, a GPU
arbiter, a cross-run remote budget ledger.

## Do not

- Write an API key into a file or a command. Hand the user the command; they run
  it. This held when asked directly and should hold again.
- Commit anything under `state/`, `jobs/`, or any `*.env` or `*.key`. All are
  git-ignored; screen `git add -A --dry-run` before any push regardless.
- Claim a benchmark result. Ten runs, zero solved. Until `optimus report` prints
  a non-zero `solved`, the honest statement is that the plane works and the
  score does not exist.

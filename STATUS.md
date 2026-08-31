# Status

Honest inventory. Modelled on Achilles's `IMPLEMENTATION_STATUS.md`, which was the
best documentation practice in either predecessor ([audit.md](docs/audit.md) §3.7):
this file says what exists, and leads with what does not.

Design: [research.md](docs/research.md) (field) → [audit.md](docs/audit.md) (predecessors) →
**[apex.md](docs/apex.md) (architecture)** → [architecture.md](docs/architecture.md) (OS plane, M6).

```bash
.venv/Scripts/python.exe -m pip install -e ".[loop,harbor]"
.venv/Scripts/python.exe -m pytest -q          # 398 passed, 3 skipped
.venv/Scripts/python.exe scripts/context_profile.py
.venv/Scripts/python.exe scripts/m3_demo.py    # the whole path, end to end
optimus status --ledger state/ledger.db
```

Optimus is **local-first**: `configs/engines.toml` declares the engines, and
`Registry.candidates` excludes every hosted one unless a caller passes
`allow_remote`. A benchmark run needs no API key at all.

```bash
optimus engines --live          # what can serve a turn, and what is refused
```

Onto the Terminal-Bench 2.0 board — the envelope step is not optional, and
[§ M3](#m3--complete) says why:

```bash
optimus keygen   --out state/owner.key

# --any-workspace, not --workspace: no Terminal-Bench task declares a workdir,
# so each one's comes from its own image and is unknown until it runs. Finding
# M3-5 below says what that widening does and does not give up.
optimus envelope --owner state/owner.key --principal you \
                 --any-workspace --venue harbor --isolation CONTAINER
export OPTIMUS_ENVELOPE=state/envelope.json
export OPTIMUS_OWNER_FINGERPRINT=<the fingerprint it printed>

# Routes over the manifest, local engines first. No key, no quota, no cost.
python scripts/bench.py --tasks 89 --attempts 5

# A hosted engine is reachable only with an explicit opt-in and a key file:
python scripts/bench.py --tasks 2 --allow-remote --env-file state/harbor.env

optimus report jobs/<job-id>
```

## Not built yet

- **There is now a score, and it is a 10-task one.** See
  [§ the ten-task run](#the-ten-task-run). `solved=3/10`, `pass@1 = 0.300`, ten
  trials, **zero harness errors and zero retries**. That is a real row and it
  should be described exactly that way: ten of the eighty-nine tasks, `k=1`,
  one local 9B. It is **not** a Terminal-Bench 2.0 board number and must not be
  quoted as one.
- **`tokens_per_solved_task` is finally finite, and it is bad.** 1,297,286.
  [apex.md](docs/apex.md) §4 targets *within 2× of Goose's 28–37K*, so this is
  roughly **18× over the ceiling** it set. The debt named in §4 is no longer
  unmeasurable; it is measured, and it is missed. See the run section for the
  single biggest reason, which is a harness problem rather than a model one.
- **The nine earlier runs each died on a harness defect** — hosted quota, an
  expired envelope, Harbor's agent timeout, then four separate
  context-accounting bugs (M3-13 to M3-16: one number, four wrong answers, each
  correct in a different layer's units).

  The tenth of those ran to completion: **40 turns, 32 of 40 gated actions settled OK,
  40 envelope uses, 3 compactions, 0 provider errors, 0 no-action turns, peak
  prompt 26,888 against a 28,672 allowance, 23 minutes, $0.00.** Its ledger
  verifies `VALID` against an out-of-band owner fingerprint across 171 rows.

  It still scored `reward: 0.0` — `qwen35-9b` did not solve `gpt2-codegolf`,
  and did not solve it again in the ten-task run either. That was the first
  failure in this project attributable to **model capability rather than to the
  harness**, which is the line M3 had to cross before a score could mean
  anything.
- **The economy is the worst number here.** 1.3M tokens per solved task against
  a 28–37K target, 89.9% of it cache hits. The largest single cause was that the
  model was never told its turn budget and so ran to the ceiling on tasks it had
  already solved. [§ the turn budget](#the-turn-budget) closes the information
  gap; **whether that actually changes the number is unmeasured** until the
  affected tasks are re-run, and it is not claimed until then.
- **Concurrency is untested against a real provider.** Every run so far has been
  one or two trials. `-n 4` and above will interact with rate limits in ways the
  backoff has not been exercised against.
- **The remote plane pins nothing.** Inside a container the Gate resolves
  lexically and cannot capture an inode, so `pins_identity` is `False` on every
  remote target and the TOCTOU window is a whole round trip rather than a
  microsecond. What bounds it is the container, which is a real wall and is
  thrown away afterwards. `gate/remote.py` says this at length rather than
  letting the weaker guarantee inherit the stronger one's name.
- **Policy does not constrain what `bash` does.** `deny-grader-script`
  pattern-matches shell text: it catches an agent that wanders into `/tests` and
  is stepped around in one substitution by an agent that means to. It is
  labelled a tripwire in `benchmark_policy()` and must never be read as a
  sandbox. Constraining shell properly needs the code-mode kernel on the remote
  plane; M2 built that for local only.
- **A local model needs Harbor's timeouts stretched.** `qwen35-9b` runs ~30
  turns in roughly 15 minutes, and Terminal-Bench's per-task agent timeout is
  900-1200 seconds. Without `--timeout-multiplier` a local run is cut off
  mid-task on wall clock rather than on capability, which measures the GPU
  rather than the harness. `bench.py` takes the multiplier and a matching
  `--max-wall-s` so the loop always stops itself first and writes a receipt.
- **Compaction is exercised but not yet stressed at benchmark length.** The
  message-pairing repair in `AgentLoop.messages` is tested directly; what has not
  happened is a 60-turn run where several compactions land mid-trajectory against
  a live provider that will reject a malformed tool-call sequence.
- **Surfaces exist but no web frontend does.** [§ M4](#m4--surfaces-partial) has
  the event bus, steering, ACP, AG-UI, REST+SSE, a terminal view and a
  pre-flight. What is deliberately absent: WebSocket (SSE instead, and named as
  such), ACP v2 (v1, negotiated), and any bundled UI — the AG-UI stream is what
  a frontend would consume and any AG-UI client already can.
- No skills/Descent, no OS plane. M5 needs a task that gets solved before
  "cost falls across runs" can be measured at all.
- **Residual TOCTOU: now POSIX is the weaker platform.** The Windows window is
  closed — see [§ M7](#m7--the-windows-check-then-open-window-closed) — by
  opening with `FILE_FLAG_OPEN_REPARSE_POINT` and verifying identity *and*
  containment on the handle rather than on the path. What is still open on
  **both** platforms is an intermediate directory swapped mid-window; Windows
  now catches it after the fact, and POSIX does not catch it at all. Bringing
  POSIX up to match, with `dir_fd`-relative opens, is the next piece.
- Compensation covers file writes and deletes. Registry, process and app-state
  inverses arrive with the OS plane; `capture()` returns `None` for them rather
  than recording a row that cannot be applied.
- Only `LocalVenue` is exercised on this host. `WslVenue`/`DockerVenue` detect
  availability and wrap argv, but are untested against live daemons here.
- **Late-emerging dependencies are evicted.** A dependency link protects an
  episode only if the link exists when compaction runs; a fact that becomes
  load-bearing 100 turns later is not protected retroactively. It stays
  *recoverable* (the summary names its id, `rehydrate()` brings it back), but the
  agent has to ask. Pinning at learn-time is the cheap fix.
- The ~4-chars heuristic is now the *fallback* only. `loop/llm.py` hands
  `ContextWindow` the model's own tokenizer through `litellm.token_counter`, so
  compaction decisions and the bill are denominated in the same units. Billed
  tokens are never estimated at all: they are read off the provider's response,
  or left at zero when the provider does not report them.
- `verify` covers events and checkpoints; it does not yet check that a checkpoint
  chain has no gaps between owners.

## M0 — complete

| Built | Where | Defends |
|---|---|---|
| Hash chain, Ed25519 event signing, owner checkpoints, merkle root | `ledger/` | audit §2.3, §2.8 |
| `verify()` requiring an out-of-band owner fingerprint; `UNATTESTED` distinct from `VALID` | `ledger/chain.py` | audit §2.3 |
| Owner/agent key separation — the Gate structurally cannot hold an `OwnerKey` | `ledger/keys.py` | audit §2.3 |
| Target resolution: lexical normalisation before I/O, component-wise containment, symlink re-check, one return | `gate/targets.py` | audit §2.1, §2.5 |
| URL resolution via `urlsplit` + `ipaddress`, with **pinned IPs** | `gate/targets.py` | audit §2.7 |
| Policy: structural deny → approval → allow, broken rule denies, load-time typecheck | `gate/policy.py` | audit §2.11, §2.14 |
| The Gate: handles not verdicts, no auto-approver, approval re-decides, reversible freeze | `gate/gate.py` | audit §2.4, §2.6, §2.8 |
| Instance-bound grants (one verb, one resolved target) | `gate/grants.py` | audit §3.2 |
| Unforgeable, single-use, expiring handles | `gate/handle.py` | apex invariant 1 |
| SDK adapter: advisory analyzer **plus** enforcing executor wrapper | `adapters/openhands.py` | finding M0-1 |

## M1 — complete

| Built | Where | Defends |
|---|---|---|
| Append-only SQLite ledger: triggers on UPDATE/DELETE, WAL, `synchronous=FULL`; `DurableChain` resumes across restarts | `ledger/store.py` | audit §2.8 |
| Typed, dependency-linked episodes with per-kind salience | `context/episodes.py` | research §4.5 |
| Priority + transitive-dependency eviction, deterministic and replayable | `context/window.py` | audit §3.4 |
| **Validated compaction** — invariants, contract, dependency closure and budget re-checked; a failing compaction is *refused* and rolled back | `context/window.py` | research §4.5 (Governance Decay) |
| `rehydrate()` / `covers()` — evicted episodes stay addressable through summary lineage | `context/window.py` | limitation above |
| Cost as a Ledger projection: tokens-per-solved-task, no-action turns, suite rollups | `meter.py` | research §2.1, apex row 19 |
| Three-mode tool policy (direct / search / code) chosen by context budget, not a flag | `tools/budget.py` | research §4.4 |

## M2 — complete

| Built | Where | Defends |
|---|---|---|
| `FileCapability` / `ArgvCapability` — identity pinned at resolve, re-verified at open, `O_NOFOLLOW` on POSIX, `O_EXCL` on create | `gate/capability.py` | M1 residual TOCTOU |
| Content-addressed blob store for prior state | `reversal/blobs.py` | — |
| `Compensator` — captures the inverse before the act, replays newest-first, skips actions that never settled | `reversal/compensator.py` | apex invariant 4 |
| Venues with honest isolation levels; `choose()` refuses rather than downgrades | `venues/base.py` | audit §2.12 |
| `LocalVenue` — allow-list env, killed process **tree** on timeout, character-safe truncation | `venues/local.py` | audit §2.12 |
| `GatedTools` — read/write/delete/list/run, every one through the Gate, denials as observations | `tools/std.py` | apex invariant 1 |
| `optimus` CLI: `keygen`, `attest`, `verify`, `undo`, `status` | `cli.py` | audit §2.3 |

`attest` is the only command that touches an `OwnerKey`, and it lives in the CLI
rather than anywhere the Gate can reach — which is the whole difference between a
receipt that proves provenance and one that proves self-consistency.

## M3 — complete

| Built | Where | Defends |
|---|---|---|
| **Autonomy envelope** — owner-signed, bounded by actor/verb/venue/workspace/ceiling/expiry; clears the untrusted-mutation invariant and nothing else, and is charged only for actions that actually happen | `gate/envelope.py` | apex invariant 3, audit §2.6 |
| Remote target resolution — lexical containment, explicit `pins_identity: False`, shell only via a declared `{"script": ...}` | `gate/remote.py` | audit §2.1, §2.5 |
| `RemoteVenue` — declared isolation, transport failure distinguished from a non-zero exit | `venues/remote.py` | audit §2.12 |
| `RemoteTools` — read/write/list/bash in a container, base64 on the wire, every one through the Gate | `tools/remote.py` | apex invariant 1 |
| `optimus why` — reads a trial or job back out of its ledger: turns, the prompt curve, refusals, breakers, and the estimate-vs-bill gap | `explain.py` | findings M3-13..16 |
| Per-turn `context.turn` telemetry — what the plane believed next to what the provider charged | `loop/agent.py` | findings M3-13..16 |
| **The loop** — turn cycle, no-action and repeat breakers, cost/wall/turn ceilings, fatal-vs-transient provider handling with provider-hinted backoff | `loop/agent.py` | research §2.1, doom-loop finding |
| **Local-first model layer** — engines and models as data; a hosted engine is excluded from every route unless a caller opts in; health check, ordered fallback, route on the ledger | `loop/engines.py`, `loop/router.py` | apex row 17, local-first |
| Model call metered as `model.call`, with the provider's own usage and cache figures | `loop/agent.py`, `loop/llm.py` | apex invariant 5 |
| Real tokenizer for budgeting; billed tokens never estimated | `loop/llm.py` | STATUS M1 limitation |
| `OptimusAgent(BaseAgent)` — ATIF trajectory, signed ledger and metrics per trial | `adapters/harbor.py` | apex §5 |
| `benchmark_policy()` — no approval rules, deny list intact, grader tripwire | `gate/policy.py` | apex §5 |
| **pass^k** alongside pass@k, joined to the verifier's verdict | `report.py` | apex §5 |
| `optimus envelope`, `optimus report` | `cli.py` | audit §2.3 |

The envelope is the piece worth arguing about, so state it plainly. Harbor runs
unattended, and the Gate's hard invariant parks every model-chosen mutation for a
human who is not there. Three easy answers were available — a `--yolo` flag, an
`auto_approve` callback, or letting the adapter mint its own assent — and all
three are the same bug: the process that wants the authorisation grants it, so
the receipt proves nothing. That is precisely Bellona's failure
([audit.md](docs/audit.md) §2.6).

What ships instead is a document the Gate can only *verify*: signed by the owner
key that lives outside every process the Gate can reach, naming one actor, one
verb set, one venue, one workspace, an action ceiling and an expiry — and
clearing exactly one thing, the untrusted-mutation invariant, before falling
straight through to the ordinary rules. Deny still denies. Irreversible still
needs assent showing the payload. Every use is a counted row naming the envelope.

And with no envelope the run still happens: every mutation comes back to the
model as a refusal, the task fails, and `operator_interventions_required` reports
exactly how many times a human would have had to intervene. That is a legitimate
row, and it is the one nobody else on the board can print.

### Findings from building it

1. **M0-1 — the SDK's `SecurityAnalyzerBase` is advisory, not authorizing.**
   `security_risk(action) -> SecurityRisk` returns a *level*; a
   `ConfirmationPolicy` decides whether to prompt, and `NeverConfirm` ships.
   Nothing in that path resolves a target or constrains what the executor then
   receives. [apex.md](docs/apex.md) §1.3 called it "the Gate's socket" — it is *a*
   socket, for confirmation UX and event history, and enforcement has to live in
   a `GatedExecutor` that only runs on a handle.

2. **M0-2 — the suite caught a bug in the shipped baseline policy.** The deny
   rule `**/.env` did not match `.env` at the workspace root, because bare
   `fnmatch` requires something before the `/`. A deny rule that silently fails
   to match is worse than no rule.

3. **M1-1 — Windows text-mode fd corrupted a private key.** `os.open` without
   `O_BINARY` translates `0x0A` to `0x0D0A`, so any Ed25519 key containing a
   newline byte was written back 33 bytes long and failed to load on restart.
   Caught by the first persistence test.

4. **M1-2 — a bad eviction policy cannot compact at all here, which is the
   point.** In `scripts/context_profile.py` the reactive condenser's every
   attempt would have evicted a safety constraint, so the validator refused all
   of them and it saved nothing. Without that validator it would have compacted
   happily and lost the constraint silently.

5. **M2-1 — `normcase` was destroying filename case.** An agent asked to create
   `CHANGELOG.md` got `changelog.md` on disk, because case folding was applied to
   the path that gets opened rather than only to the key used for comparison.
   That is a different file to git and to every case-sensitive system
   downstream. **The 115-test suite did not catch it; running the demo did** —
   which is the argument for exercising the thing end to end at every milestone
   rather than trusting green tests.

6. **M3-1 — the Gate's best invariant is exactly what blocks an unattended run.**
   `_decide` sends every untrusted-origin mutation to `NEEDS_APPROVAL` before the
   rules are consulted, which is correct and is the property Achilles got right.
   It also means a Terminal-Bench trial parks on turn one and scores zero.
   Discovering that the *right* design is the obstacle is the useful kind of
   finding: the fix could not be a flag without destroying the property, so it
   had to be a signed, bounded, verifiable document instead. The invariant is
   unchanged; what changed is that there is now one narrow, auditable way to
   stand in for the human.

7. **M3-2 — eviction and the chat wire format disagree about what an atom is.**
   `ContextWindow` evicts an action and its observation independently, on
   purpose, because they carry different salience. Every provider rejects an
   assistant tool call with no matching result — and the reverse. Weakening the
   eviction policy to keep them paired would have thrown away the thing that
   makes the plane good, so the repair happens at render time instead:
   `AgentLoop.messages` demotes an orphaned half to plain text, deterministically.

8. **M3-3 — Harbor's `environment.exec` already wraps its argument in
   `bash -lc`.** `RemoteResolver` also builds `(bash, -lc, script)`, because on a
   transport that takes a real argv that is the honest representation. Sending
   both double-wraps and silently changes the quoting — the same class of bug as
   Bellona interpolating booleans into CEL source. Caught by reading Harbor's
   source rather than by a test, which is the argument for reading the thing you
   are mounting.

9. **M3-4 — the standing rules were being sent twice, every turn.** They live in
   the system block *and* were rendered out of the context window as user turns,
   because the renderer's fallback for an episode with no message is to make one.
   Roughly 120 tokens per turn in the one part of the prompt that prompt caching
   does not help — on a harness whose entire published claim is tokens per solved
   task. The tests were green throughout; printing the actual message list is what
   showed it, which is finding M2-1 again in a different costume.

10. **M3-5 — a benchmark suite has no single workspace, so the envelope needed a
    venue scope.** None of Terminal-Bench 2.0's 89 tasks declares a `workdir`;
    each inherits its own image's `WORKDIR`, discovered by `pwd` once the
    container is up. An envelope naming a fixed path would have covered none of
    them. The fix is an explicit `ANY_WORKSPACE` (`"*"`) written *into the signed
    document*, so `envelope.opened` states the scope rather than leaving an
    auditor to infer it from a blank field, and the CLI makes it a deliberate
    `--any-workspace` rather than a default. It is a genuine widening and worth
    being clear about: what it drops is a second, defence-in-depth check on the
    path. What still bounds the run is the venue clause plus `RemoteResolver`'s
    containment — the *primary* check, built by harness code from the container's
    real working directory — along with verbs, ceiling, expiry and actor.

11. **M3-6 — a denied action was spending the envelope's action ceiling.**
    Coverage was checked and charged in one call, before policy ran, so an
    attempt the rules then refused still cost the operator a unit. Wrong twice:
    the field is `max_actions` and no action occurred, and an agent repeatedly
    attempting refused work could burn the whole budget without ever doing
    anything. Checking and spending are now two calls, and the charge lands only
    once the verdict is ALLOW. Found by a test written to assert something else
    entirely — the deny rules still holding under a venue-scoped envelope.

12. **M3-7 — failed model calls were being published as no-action turns.** The
    first real Terminal-Bench run reported `no-action turns/task 3.00` when the
    model had idled *zero* times: three calls had been rate-limited, and
    `no_action` was computed as `not acted`, which is true of a call that never
    reached the provider. That is the single metric this project exists to
    publish (`research.md` §2.1), and the figure was both wrong and
    incomparable to Goose's. `ModelReply.idled` is now
    `not error and not tool_calls`, and provider errors are counted separately
    in the meter, the receipt and the report. **202 unit tests were green while
    this was broken.** Only a real run against a real provider surfaced it —
    finding M2-1's lesson, for the third time.

13. **M3-8 — transient and fatal provider failures were treated identically.**
    One `max_provider_errors=3` counter covered both, so a 404 for a retired
    model burned three attempts and ten minutes to receive the same answer three
    times, while a 429 killed a trial that had already completed eleven good
    turns and twelve gated actions. Now split: fatal (404/401/400) stops at
    once; transient (429/5xx/timeouts) gets exponential backoff with a tolerance
    of eight, honours the provider's own `Please retry in Ns` hint as a floor,
    and is deliberately *not* pushed into the model's context — a rate limit is
    the harness's problem and telling the model spends tokens to teach it
    nothing. Unknown exceptions classify as transient, because abandoning a run
    is the costlier mistake and the error budget bounds it.

14. **M3-9 — the envelope's door said yes to an expired envelope.** `verify()`
    checked the signature and the fingerprint but not the expiry; only
    `covers()` did, per action. A real local trial therefore opened a
    two-day-expired envelope, logged "envelope opened", and then correctly
    refused all 31 of its actions one at a time across nine minutes of
    inference and 232K tokens. Every downstream component behaved perfectly —
    which is precisely why it went unnoticed for so long. Expiry is now checked
    at admission, `bench.py` refuses to start a run whose envelope expires
    within half an hour, and the per-action check stays for the run that
    outlives its own grant.

15. **M3-10 — nothing stopped a run in which every action was refused.** The
    loop breaks doom loops and repeats, but had no notion of "the Gate is
    refusing everything, so this run cannot succeed". That is a distinct
    pathology from an idle turn — the model is working, thinking, and varying
    its commands; it simply has no authority to do anything. `max_consecutive_denials`
    now stops it with `blocked`. A single refusal among successes resets the
    streak, because being told no once is normal and is supposed to be an
    ordinary observation the agent adapts to.

16. **M3-11 — a killed trial threw away numbers the ledger still had.** Harbor
    cut a local trial off at its 900-second agent timeout, mid-turn, and the
    trial reported no tokens, no cost and no actions — while 233KB of signed
    ledger sat on disk holding every one of them, because the receipt was only
    written on the way out of `run()`. Harbor provides
    `populate_context_post_run` for exactly this, and it is now implemented:
    the whole receipt is rebuilt by folding the ledger. That this is *possible*
    is invariant 2 (`apex.md` §3) paying for itself — the Ledger is the system
    of record and the metrics file is a projection, so losing the projection
    loses nothing. Rebuilt receipts are flagged `reconstructed_from_ledger`
    and say `killed_before_finishing` rather than inventing a stop reason the
    loop never recorded.

17. **M3-12 — the loop kept driving a GPU after the harness had given up.**
    `asyncio.to_thread` cannot be cancelled, so when Harbor timed out at 900
    seconds and moved on, the worker thread carried on to the loop's own
    1800-second ceiling — burning local inference for a trial whose result had
    already been recorded. At suite scale that orphans one loop per timed-out
    task. The loop now takes a `threading.Event` and stops at the next turn
    boundary; the adapter sets it on cancellation and waits briefly for the
    receipt. Cooperative rather than forced, because killing the thread would
    leave a half-written ledger and no numbers at all.

18. **M3-13 — the context plane budgeted in one unit and was billed in another.**
    The worst bug of M3, in the plane the whole project is named for.
    `ContextWindow.used()` sums `episode.content`, which is the right unit for
    the plane and the wrong one for the wire: the rendered request also carries
    the system block, the standing rules, the tool schemas (~370 tokens, sent
    every single turn) and a JSON envelope around every message and tool call.
    On a real local run the gap was about 3,000 tokens — the window believed it
    was inside a 24,768-token allowance while llama.cpp was receiving 28,025,
    **compaction fired zero times across 25 turns**, and the server eventually
    refused with "Context size has been exceeded" after 15 of 16 actions had
    succeeded. Compaction is now driven by `AgentLoop.prompt_tokens()`, which
    measures the request that will actually be sent, and loops until *that*
    fits. `context.compacted` records `rendered_before`/`rendered_after`
    alongside the episode counts, because the episode numbers were never the
    ones that mattered. The reserve also follows the routed model's declared
    `max_output_tokens` instead of a fixed 8K: holding back twice what a model
    can emit throws away context that was paid for.

19. **M3-14 — measuring the right thing was not enough; the ruler was wrong.**
    M3-13 pointed compaction at the rendered request, and the very next local
    run still overflowed: 31,921 tokens into a 32,768 window while the loop
    believed it was under a 28,672 allowance. Two causes, neither fixable by
    being more careful in this process. `litellm.token_counter` does not know a
    local model id and silently falls back to a generic tokenizer. And the
    server renders the conversation *and the tool schemas* through its own
    Jinja chat template, which is a different and larger string than the JSON
    this side can see. The estimate was low by about a third.

    The fix is to stop guessing and calibrate against the only authority that
    knows: the `prompt_tokens` the provider reports for the call just made.
    `AgentLoop` keeps a correction ratio, updated from real usage, recorded as
    `context.calibrated` so it is auditable rather than a fudge factor.
    Deliberately asymmetric — an under-estimate ends the run outright while an
    over-estimate only compacts early, so a correction upward is taken
    immediately and downward decays slowly, and it is clamped at 1.0 because
    claiming a prompt is smaller than what we can already see is arguing with
    arithmetic.

    Worth naming as a pattern: this is the same shape as M1's decision to read
    billed tokens off the provider rather than estimate them. Anywhere the
    provider knows a number, that number is the truth and ours is a prior.

20. **M3-15 — a ratio was the wrong model, and the next run proved it.** The
    calibration in M3-14 was learned as 1.22 from an 838-token prompt and then
    applied multiplicatively. But the error it was correcting is mostly
    *additive* — a fixed chat-template and tool-schema overhead — so a factor
    that is right at 838 tokens (where ~185 tokens is 22%) is meaningless at
    25,000 (where it is under 1%). The correction decayed back toward 1.0 while
    the real prompt climbed 28,160 → 30,966 → 32,424, compaction fired zero
    times again, and the server refused. Twenty-three of twenty-eight gated
    actions had already succeeded.

    The budget is now *anchored* rather than extrapolated: take the size the
    provider reported for the last call, and add the episode tokens added
    since. Compaction moves it back down by the same arithmetic, because the
    delta is signed. Ground truth plus a measured delta, instead of a ratio
    fitted at one scale and used at another.

    Three attempts at this one number, which is worth recording plainly: the
    first measured the wrong thing, the second measured the right thing with a
    broken ruler, and the third stopped measuring and started asking.

21. **M3-16 — and then detection was right while eviction did nothing.** With
    the anchored budget in place the loop finally saw the problem exactly, and
    said so on seven consecutive turns: *"rendered request is 29,623 tokens
    against an allowance of 28,672, and nothing further is evictable."* It was
    wrong about the last clause. `ContextWindow.compact()` sizes its eviction
    against `budget.target`, measured in `used()` — episode tokens — and by
    that measure the window was comfortably under target, so the eviction loop
    broke on its first iteration and returned `evicted=0`. Every layer was
    behaving correctly in its own units, and the units were the bug for the
    fourth time.

    `compact()` now takes an explicit `target`, and the loop computes one by
    scaling the episode total by how far the *rendered* request has to fall —
    to 70% of the allowance rather than to the brim, because compacting to
    exactly the limit means the next observation overflows immediately.

    The pattern across M3-13 through M3-16, worth stating once: **a plane that
    is correct in its own units is not correct.** Every one of these four was a
    component doing precisely what it was designed to do, measured in a unit
    that nothing downstream was billed in.

22. **M3-17 — `write_file` cannot declare `COMPENSATION` on the remote plane.**
   `Compensator.capture` reads prior state off *this* filesystem, and there is no
   inverse to capture across a container boundary. It is declared `SNAPSHOT`,
   which is true: what reverses a Harbor trial is discarding the container.
   Declaring the stronger type would have put inverse rows in the ledger that
   nothing could ever apply.

23. **M3-18 — the first 10-task run lost 8 trials to DNS, and nothing retried
    them.** Not a harness defect, and worth recording precisely because of
    that. Eight of ten tasks needed container images that were not yet cached,
    Docker could not resolve `registry-1.docker.io`, and their environments
    never built — so the agent was never reached. Harbor's default of
    `--max-retries 0` meant every one of them logged *"Not retrying trial
    because the maximum number of retries has been reached"* and stopped.

    Two things now guard it: `bench.py` defaults to 2 retries, and its
    preflight pings the registry and says plainly that uncached tasks will fail
    — warned about rather than refused, because a fully cached image set can
    legitimately run offline.

    The part that worked is the part worth keeping: `optimus report` refused to
    average over the survivors. It printed *"8 trial(s) have no Optimus
    receipt; token figures below cover only the rest"* rather than presenting a
    79.3% cache-hit rate measured on 2 trials as though it covered 10. That
    guard was written two days earlier for exactly this shape of situation and
    this is the first time it fired on real data.

### Measured (mechanism, not benchmark)

150-turn synthetic trajectory, 32K window:

| strategy | billed input | peak | compactions | invariants | late dep |
|---|---:|---:|---:|---:|---|
| no compaction | 4,852,544 | 64,223 | 0 | 2 | kept |
| positional (reactive condenser) | 4,852,544 | 64,223 | 0 (all refused) | 2 | kept |
| **optimus (priority + dependency)** | **2,774,443** | **27,887** | 4 | 2 | recoverable |

**1.7× on this trajectory**, and the number that matters more is `peak`: bounded
at 27,887 against unbounded growth, so the gap widens with run length rather than
staying constant. This says nothing about pass rates. Real
tokens-per-solved-task comes from Harbor, and the target from
[apex.md](docs/apex.md) §4 stands: within 2× of Goose's 28–37K at ≥ its pass rate.

### The first complete Terminal-Bench trial

Run 10, `gpt2-codegolf` on `qwen35-9b` through the local llama.cpp router, no
API key involved:

```
40 turns | 32 of 40 gated actions settled OK | 40 envelope uses
3 compactions: rendered 31,288 -> 25,830 | 30,781 -> 24,412 | 30,839 -> 25,454
peak prompt 26,888 against a 28,672 allowance   (never overflowed)
0 provider errors | 0 no-action turns | 79.7% cache hits | 23 min | $0.00
ledger: VALID - records=171 chain=True signatures=True owner_match=True
verifier reward: 0.0
```

The reward is the honest part: the model did not solve the task. Everything
else is the harness doing its job — bounded context, every action authorised
against a signed envelope, every turn metered, the repeat breaker firing twice
on a genuine loop, and a receipt that verifies against a fingerprint held out
of band. **This is a mechanism result, not a benchmark result.**

`scripts/m3_demo.py` runs the whole M3 path — owner key, signed envelope, loop in
a real shell, attest, verify, report — and prints the row that would go on the
board. It is a **mechanism check, not a benchmark**: the model is scripted, the
task is trivial, and the venue is a bare process rather than a container.

```
run=demo-run stop=finished turns=7 tokens=13,230 (cache 8,400) no_action=1 denials=1
ledger 28 rows; VALID chain=True signatures=True attested_through=27 owner_match=True
  effect.settled=5  envelope.opened=1  envelope.used=5  gate.decision=5
  gate.refused=1  loop.breaker=1  model.call=7  run.started=1  run.finished=1

  pass@k k=1: 1.000        pass^k k=1: 1.000
  tokens/solved-task      13,230
  no-action turns/task    1.00
  cache hit rate          66.7%
  unsafe attempts refused 1
  operator interventions  0
```

The only things that number proves are the ones worth proving before spending
money: every effect reached the ledger, the chain verifies against an out-of-band
owner fingerprint, the escape attempt was refused, the idle turn was counted and
corrected, and the shape of the published row is real. **The pass rate is
meaningless — one scripted trial.** Replace the model and the dataset and the
same code prints the row that counts.

## The ten-task run

Ten Terminal-Bench 2.0 tasks, `qwen35-9b` locally, one attempt each, 40 turns,
serial. The run the whole of M3 was building toward.

```
tasks=10  trials=10  solved=3
  pass@k k=1: 0.300
  pass^k k=1: 0.300
  tokens/solved-task      1,297,286
  cost/solved-task        $0.0000
  no-action turns/task    1.40
  cache hit rate          89.9%
  unsafe attempts refused 0
  operator interventions  0
```

**Zero errored trials and zero retries.** The previous attempt lost 8 of 10 to a
transient DNS failure pulling images (M3-18); the retry defence and the registry
preflight held, and were not even needed. That was the result this run was
bought to get, and the three solved tasks were not expected at all — every
document in this repo, including this one, predicted `solved=0`.

| task | stopped | turns | tokens | ok/act | solved |
|---|---|---|---|---|---|
| largest-eigenval | finished | 40 | 396,495 | 37/38 | **yes** |
| log-summary-date-ranges | max_turns | 40 | 430,650 | 40/40 | **yes** |
| pytorch-model-cli | max_turns | 40 | 371,262 | 32/41 | **yes** |
| break-filter-js-from-html | finished | 19 | 60,064 | 18/18 | no |
| write-compressor | stalled | 16 | 252,585 | 9/9 | no |
| gpt2-codegolf | max_turns | 40 | 583,897 | 29/33 | no |
| llm-inference-batching-scheduler | max_turns | 40 | 674,261 | 32/36 | no |
| merge-diff-arc-agi-task | max_turns | 40 | 363,476 | 32/41 | no |
| reshard-c4-data | max_turns | 40 | 425,222 | 36/40 | no |
| winning-avg-corewars | max_turns | 40 | 333,945 | 39/41 | no |

The three solved are the three smallest images in the set, which is the pattern
one would want to see: a 9B solved the easy end and failed the hard end, rather
than scoring somewhere inexplicable.

### The finding that matters more than the score

**Two of the three solved tasks never called `finish` at all**, and the third
called it on turn 40 — the last turn available. `log-summary-date-ranges` and
`pytorch-model-cli` ran `bash` every turn from 33 to 40 and stopped only because
the ceiling arrived. Meanwhile the one run that called `finish` early —
`break-filter-js-from-html`, at turn 19 — was wrong, and had not solved it.

Reading the ledgers gave the reason, and it is a harness omission rather than a
model failing: **nothing ever told the model how many turns it had.** Not the
system prompt, not the conversation. It could not distinguish turn 3 from turn
39, so there was no point at which concluding became the obvious move.

The cost is the whole of the apex §4 debt. A solved task burns 40 turns instead
of the dozen it needed, and because prompt size grows every turn the token bill
grows super-linearly with the waste. It is also worth being precise about what
finishing early buys: **tokens, never score.** The verifier grades the container
either way, which is exactly why both tasks that ran out of turns were still
marked solved. That asymmetry is what makes the fix delicate — see
[§ the turn budget](#the-turn-budget).

### Integrity

Each trial's ledger verifies `UNATTESTED`: chain intact, every signature valid,
`failures=0`, across ~205 rows per trial. `UNATTESTED` rather than `VALID` is
the *correct* state and not a defect — it means no owner checkpoint has been
signed over the chain, and `attest` is deliberately the one operation the
benchmark process cannot perform, because it is the only thing that touches an
`OwnerKey`. An operator turns these into `VALID` with one command per trial.

`unsafe attempts refused 0` and `operator interventions 0` across all ten: the
grader tripwire never fired and no action was ever parked, so nothing in this
run needed a human.

## The turn budget

The fix for the finding above, and it is two lines of information rather than a
mechanism:

1. **The system prompt states the budget** — "you have at most N turns" — which
   is constant for a run and therefore stays inside the cacheable prefix. One
   line, once, and cache hits are the only reason a 40-turn run is affordable.
2. **Notices as the budget runs low**, at 10 and 3 turns remaining by default
   (`LoopLimits.budget_notices`). Two marks rather than one per turn: forty
   extra messages would be a real charge against a budget this project spends
   considerable effort bounding, and the information only changes anything near
   the end. Each is pushed *before* the compaction check, so it is inside the
   allowance it is announcing rather than one turn late.

**What the notice deliberately does not do is suggest the model is finished.**
It reports the turn count and what happens at the ceiling, and stops. The
restraint is the whole design, because the payoff is asymmetric: finishing early
buys tokens and never score, while a premature `finish` on a task that would
have been solved by turn 30 throws away a solve. That failure is not
hypothetical — `break-filter-js-from-html` called `finish` at turn 19 and had
not solved it. A notice that nudged toward concluding would make that more
common and would be trading solves for tokens, which is the wrong direction. So
the harness hands over a number the model cannot otherwise see, and leaves the
judgement where it belongs.

14 tests cover the plumbing — when a notice fires, that each mark fires once,
that a short run never sees one, that it does not stop the run by itself, that
it stays evictable rather than becoming an invariant, and that its wording
contains none of the phrases that would be steering. None of that shows a real
model behaves differently, which is the only thing that matters, so the affected
tasks were re-run (`bench.py --include <task>`, added for this).

### Measured: one task transformed, one task worse. Inconclusive.

Both affected tasks re-run, same model, one attempt each:

| task | before | after |
|---|---|---|
| `log-summary-date-ranges` | 40 turns, 430,650 tokens, **solved** | **14 turns, 109,348 tokens, solved** |
| `pytorch-model-cli` | 40 turns, 371,262 tokens, **solved** | 40 turns, 531,501 tokens, **not solved** |

The first is exactly what the change was for, and the mechanism is not the one
that was built. `loop.budget_notice` rows for that trial are **empty** — the run
ended at turn 14 and the first mark would not have fired until 31. The whole
effect came from the single static line in the system prompt saying the budget
exists. One sentence, 321,302 tokens.

The second is the honest half. It behaved as though the change were not there:
still 40 turns, still no `finish`, **both notices fired and were ignored** (turn
31 and turn 38), 43% *more* tokens, and the solve lost. Whatever happened there,
it is not the failure mode that was feared — a nudge talking the model into
stopping early — because the model never stopped early. The trajectory simply
diverged and went worse.

**So: one trial each, one large win, one loss, and no way to separate the change
from run-to-run variance.** A local model samples stochastically and
Terminal-Bench rewards are binary, so a single trial per arm cannot distinguish
"this helped" from "this task is near its threshold and flipped". The 43% token
increase is trajectory divergence rather than notice overhead — two notices are
about 60 tokens each and cannot account for 160,239 of them.

This was written down as a caveat before the second trial landed, and the second
trial is why the caveat exists. **Nothing here is a before/after for the suite.**
What would make it one is `-k 5` on the affected tasks, which is now cheap:
`bench.py --include <task> --attempts 5`.

The change stays in on the argument that it closes a genuine information gap the
ledgers showed — the model could not see its own budget — and not on the
strength of this evidence, which does not support a claim either way.

## M7 — the Windows check-then-open window, closed

Carried as a residual since M1: POSIX gets `O_NOFOLLOW`, Windows got nothing, so
between the identity check and the `open()` a reparse point could be substituted
and the open would follow it. `gate/winfile.py` closes it, and the design
changed twice while reading the actual API contracts.

**It does not use `NtCreateFile`, and that is the right call.** The roadmap said
it would need "`NtCreateFile` with `FILE_OPEN_REPARSE_POINT` through a native
module". But `CreateFileW` already takes `FILE_FLAG_OPEN_REPARSE_POINT`
(0x00200000), documented as "normal reparse point processing will not occur" —
the `O_NOFOLLOW` equivalent, on a supported API, reachable from `ctypes`. The
`Nt*` family is the undocumented layer underneath; reaching for it when the
documented call does the job trades a stability guarantee for nothing.

**And the flag was not the important part.** The real fix is *where* the check
happens. The old code verified identity on a **path** and then opened that path
— two operations against a name something else can re-point in between. Now the
file is opened first and verified **on the handle**: identity from
`GetFileInformationByHandleEx`, real location from `GetFinalPathNameByHandleW`.
Once the handle is held the name cannot be re-pointed underneath it, so the
object checked is necessarily the object read. Open-then-verify closes what
check-then-open leaves open.

### Findings

30. **M7-1 — the plausible identity field is the wrong one.**
    `BY_HANDLE_FILE_INFORMATION.dwVolumeSerialNumber` is 32-bit and does **not**
    equal `st_dev`; CPython takes `st_dev` from the 64-bit `FILE_ID_INFO`.
    Measured on this host: 2,094,255,989 versus 14,302,539,503,512,047,477.
    Comparing the obvious one would have refused every open of a perfectly
    correct file — a security check that fails closed on everything is still a
    broken check. This is M3-13..16 one layer down: two numbers that mean the
    same thing in different units.

31. **M7-2 — the first version of the security test proved nothing.** It put a
    junction at the *final* component and asserted a refusal. Disabling the new
    module showed the refusal happened anyway: the resolver catches a junction
    that exists at resolve time, and a directory junction cannot be opened as a
    file regardless. The test passed identically with and without the thing it
    was testing. Rewriting it to force the swap *inside* the window, on an
    intermediate directory, produced the real result: **without the module the
    attack reads back a file from outside the workspace and is not refused at
    all.** Every security test in this project should be run once with its
    defence disabled, and this one is why.

### What remains open

An intermediate directory swapped mid-window is now **caught but not
prevented** — the open succeeds and the containment check on the handle refuses
it afterwards, so nothing is read and a bad create is removed. Preventing it
needs component-by-component relative opens (`openat`;
`OBJECT_ATTRIBUTES.RootDirectory`). POSIX has the same gap **and does not catch
it**, which is now the weaker of the two platforms and should be brought up to
match.

## M4 — surfaces, partial

Everything M4 needs turned out to hang off one primitive the loop did not have:
a per-turn event stream. The loop was built to be driven by a benchmark harness,
which wants one trajectory, once, at the end. Every other way of using an agent
wants the opposite — the turn happening now, and a way to change its mind before
the next one. So `surface/` adds exactly two things and makes everything else a
projection of them.

| Built | Where | Notes |
|---|---|---|
| `Bus` — publish/subscribe, never blocks the loop, drops **visibly** | `surface/events.py` | bounded per subscriber; a monotonic `seq` makes a gap arithmetic rather than a guess |
| `Control` — priority-queue steering, interrupt, cancel | `surface/control.py` | owns the `threading.Event` the loop already polls, so the Harbor adapter is unchanged |
| Loop integration | `loop/agent.py` | mirrors the bus **from the single point that writes the ledger** |
| AG-UI emitter | `surface/agui.py` | checked against the protocol's own wire values, not its prose tables |
| ACP agent over stdio | `surface/acp.py`, `optimus acp` | v1, negotiated honestly; `session/request_permission` wired to the Gate |
| REST + SSE | `surface/server.py` | loopback-only and token-gated by default |
| Terminal view | `surface/tui.py` | ANSI, not `curses` — which is not in the Windows stdlib |
| Pre-flight dry-run | `surface/dryrun.py` | runs against a *shadow* Gate, so a preview costs nothing |

**398 tests, 3 skipped across the project. Ruff clean.** `optimus acp` handshakes over real pipes:
`initialize` → `session/new` → a proper `-32601` for an unknown method.

The bus mirror is the design decision worth defending. Publishing from
`_record` — the one function that writes the ledger — rather than from a dozen
call sites means a live surface and a post-hoc `optimus why` are reading the
same row under the same name. The worst class of bug in M3 was two renderers
each deriving "what happened" from their own reading, and the same trial
reporting 40 turns in one view and 41 in the other. This makes that particular
disagreement unrepresentable rather than merely absent.

**The bus is lossy and the ledger is not.** A subscriber that cannot keep up
loses events, deliberately: a terminal must never be able to slow the loop, and
the alternative to dropping is back-pressure that does exactly that. What it
will not do is lose them quietly — every subscription counts its own drops, and
the TUI prints the count rather than showing a gap-free-looking picture.

### Not built, and named rather than glossed

- **SSE, not WebSocket.** apex §7 says "REST/WS core". Hand-rolling RFC 6455 to
  earn the letter would be a rebuild of a solved thing; adding an ASGI stack
  would make a project whose entire runtime dependency is `cryptography` need a
  web framework. SSE is AG-UI's own default transport and costs nothing. So the
  document says SSE (house rule 5).
- **ACP v1, not v2.** v2 is a substantial redesign — `session/prompt` no longer
  signals turn completion, `fs/*` and `terminal/*` are gone. A client asking for
  v2 is answered `protocolVersion: 1` and decides for itself. v1-only agents are
  expected to stay common for some time, and a half-v2 that fails inside an
  editor is worse than an honest v1.
- **No web frontend.** The AG-UI stream is what one would consume, and any
  AG-UI client can already consume it. Writing one is not the bottleneck.
- **`allow_always` is offered and not remembered.** ACP defines the option and
  editors display it. Honouring it would mean this process deciding that some
  future action needs no human, which is the invariant the Gate exists to hold.
  It is treated as `allow_once` and the downgrade is recorded rather than
  dropped.
- **The token ceiling in CI is still blocked**, for the same reason as before:
  nothing has been solved, so `tokens_per_solved_task` is `inf` and there is no
  baseline to regress against.

### Findings from building it

24. **M4-1 — a payload whose keys are data met a signature whose keys are
    structure.** The bus mirror splatted ledger payloads as `**kwargs` into
    `publish(kind, *, turn, ...)`. Ledger payloads are arbitrary dicts:
    `loop.breaker` rows carry their own `kind`, and every row carries `turn`. So
    the first breaker of the first stalled run raised `TypeError` — on a path no
    happy-path test takes. Fixed structurally, by taking the payload as a dict
    rather than as keywords. Same family as M3-13..16: two things each correct
    in their own frame, wrong where they met.

25. **M4-2 — handling `session/prompt` on the reader thread deadlocks the
    permission round trip.** `session/prompt` blocks for a whole run.
    `session/request_permission` is an outbound request made *from* the turn,
    whose reply arrives on the reader thread. Handle the prompt inline and the
    turn waits for a message only the thread it is blocking could read. The same
    bug meant `session/cancel` was not read until the run it was meant to cancel
    had already finished — a stop button that starts working once it is
    pointless. Both need a parked action or a cancel *during* a live prompt to
    appear at all, so neither shows up in a test that drives one method at a
    time. Requests now run on their own threads, and a regression test answers a
    permission request mid-turn over real pipes.

26. **M4-3 — the end-of-stream sentinel was dropped for exactly the subscriber
    that needed it.** `close()` delivered it with `put_nowait`, which fails when
    the queue is full — and a full queue is precisely the subscriber that fell
    behind. Its consumer then blocked forever on a `get()` nothing would answer:
    a thread hung for the life of the process, on the slow surface the drop
    policy exists to tolerate. The sentinel now evicts to make room. A drop
    policy has to have an exception for the message that says there will be no
    more messages.

27. **M4-4 — `stop()` on a server that was never started hung forever.**
    `BaseServer.shutdown()` waits for `serve_forever()` to acknowledge, and
    `serve_forever()` is what would set that flag. Binding in `__init__` and
    serving in `start()` is what makes it reachable, and that split is worth
    keeping — the port has to be known before the run starts so it can be
    printed — so `stop()` is idempotent and start-aware instead.

28. **M4-5 — AG-UI's documentation tables name the TypeScript classes, not the
    wire values.** The tables say `RunStarted` and `TextMessageStart`; the `type`
    discriminator carries `RUN_STARTED` and `TEXT_MESSAGE_START`. An emitter
    written from the tables passes every test that asserts against itself and is
    rejected by every real client. Reading the SDK's own enum took one request,
    and was the difference between a working emitter and a plausible one.

29. **M4-6 — a benchmark policy has no approval rules, and an editor needs
    them.** `benchmark_policy()` deliberately contains none: under Harbor there
    is nobody to approve, so a parked ticket is only a slower refusal. `optimus
    acp` uses `baseline_policy()`, which stages writes and approves execution —
    and every one of those parked tickets becomes a question ACP puts to the
    person in the editor. Same Gate, same invariant, and **no envelope needed at
    all** when there is a human in the room: the assent then names what they
    were actually shown rather than a scope agreed in advance. That is the first
    place in this project where the interactive story is the *stronger* one.

## Next

**M5–M7 are not started.** In the order the evidence says to do them:

- **M5 — skills and graduation.** Done when a repeated task's cost falls
  measurably across runs. That needs tasks that get solved, so it is blocked
  behind the same thing as the token ceiling.
- **M6 — the OS/desktop plane.** [architecture.md](docs/architecture.md) puts
  the Agent Workspace feasibility spike first, and nothing here changes that.
- **M7 — multiplayer, durability, OS-level policy enforcement**, and the
  `NtCreateFile` work that closes the residual Windows TOCTOU window.

The nearest useful work remains a model that can solve something. `qwen38-27b`
is declared and unloaded; until *something* is solved, `tokens_per_solved_task`
is `inf` and the CI ceiling from [apex.md](docs/apex.md) §4 cannot be written.

# Status

Honest inventory. Modelled on Achilles's `IMPLEMENTATION_STATUS.md`, which was the
best documentation practice in either predecessor ([audit.md](docs/audit.md) §3.7):
this file says what exists, and leads with what does not.

Design: [research.md](docs/research.md) (field) → [audit.md](docs/audit.md) (predecessors) →
**[apex.md](docs/apex.md) (architecture)** → [architecture.md](docs/architecture.md) (OS plane, M6).

```bash
.venv/Scripts/python.exe -m pip install -e ".[loop,harbor]"
.venv/Scripts/python.exe -m pytest -q          # 262 passed, 2 skipped
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

- **No Terminal-Bench score, and the reason has finally changed.** Ten runs
  against the real dataset in real Docker containers. The first nine each died
  on a harness defect — hosted quota, an expired envelope, Harbor's agent
  timeout, then four separate context-accounting bugs (M3-13 to M3-16: one
  number, four wrong answers, each correct in a different layer's units).

  The tenth ran to completion: **40 turns, 32 of 40 gated actions settled OK,
  40 envelope uses, 3 compactions, 0 provider errors, 0 no-action turns, peak
  prompt 26,888 against a 28,672 allowance, 23 minutes, $0.00.** Its ledger
  verifies `VALID` against an out-of-band owner fingerprint across 171 rows.

  It still scored `reward: 0.0` — `qwen35-9b` did not solve `gpt2-codegolf`.
  That is the first failure in this project attributable to **model capability
  rather than to the harness**, which is the line M3 had to cross. What does
  not yet exist is a *score*: one unsolved task on one model is not a
  benchmark row, and `tokens_per_solved_task` is still `inf` because nothing
  has been solved.
- **617K tokens for one unsolved 40-turn task.** 79.7% of it cache hits, which
  is the only reason it is affordable at all, and free because it is local. But
  [apex.md](docs/apex.md) §4 targets *within 2× of Goose's 28-37K per solved task*,
  and there is no honest way to compare an unsolved run to that. The next real
  economy question is per-turn growth, and it is M4 work.
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
- No surfaces, no skills/Descent, no OS plane.
- **Residual TOCTOU on Windows.** File identity is pinned at resolve and
  re-checked at open, and POSIX adds `O_NOFOLLOW` — but Windows has no `dir_fd`
  and no `O_NOFOLLOW`, so a microsecond window remains between the check and the
  open. Closing it needs `NtCreateFile` with `FILE_OPEN_REPARSE_POINT` through a
  native module. M7, and not pretended to be done.
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

## Next

M4: surfaces. TUI and web over the REST/WS core, priority-queue steering,
confidence signalling, pre-flight dry-run, ACP client, AG-UI emitter — plus the
per-turn callback that lets `AgentContext` survive a killed trial, and the
tokens-per-solved-task ceiling enforced in CI, which [apex.md](docs/apex.md) §4 puts
at M4 and which cannot be written until a real Harbor run has produced a baseline.

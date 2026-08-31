# Optimus — Pass 2: Reality Check

Audit of **`E:\bellona`** (Rust, 17,258 LOC, 15 crates) and **`D:\local-sovereign-ai`** / *Achilles* (Python, 17,156 LOC, ~100 modules), measured against the Aug-2026 yardstick in [research.md](research.md).

Read [research.md](research.md) §4 first; this document assumes it.

---

## 0. The verdict, before the detail

**Bellona has the better architecture and the worse code. Achilles has the better code and the weaker architecture.**

Bellona's design is genuinely closer to the 2026 frontier than most shipped open source: a single mandatory gate, CEL policy with structural deny-before-allow, a hash-chained ledger with a third-party verifier, registration≠exposure, tool promotion gated on a proving battery, pass^k eval gates with cost budgets. On paper it is a better harness than Goose. In reality:

- **It does not compile.** `cargo check --workspace` fails with 9 errors in `manus`. The binary cannot be built on its primary target platform today.
- **It has a proven arbitrary-file-write escape** in the single function every file tool depends on.
- **Its identity system signs its own attestations with keys it mints on demand** — the "owner countersignature" proves nothing.
- **Its audit ledger is in-memory** and dies with the process.
- Roughly a third of its surface — the WASM plugin host, the context window, three of four sandbox rungs, the MCP/ACP traits — is unwired, unimplemented, or orphaned.

Achilles is the opposite. Its ambitions are narrower and its claims are audited (the README lists its own gaps before its features). And:

- **255/255 tests pass** (verified: `pytest tests/ -q`, 130s, WSL).
- Its **trust-label enforcement is real and consistently applied** — every mutating tool authorizes with `trust=UNTRUSTED_MODEL_OUTPUT`, and the policy engine can never let untrusted-origin input authorize a mutation. This is the CaMeL/dual-LLM boundary, correctly built, in production code. Neither Claude Code nor Codex ships this.
- Its **DiffSandbox** — approve the diff, not the permission class — is the single best idea in either repository and answers a 2026 open problem directly.
- Its workspace containment is **correct** (canonicalize, longest-match, `relative_to`) where Bellona's is broken.

Neither is close to "best open source harness in the world" yet. Bellona is a beautiful cathedral with no floor. Achilles is a well-built two-storey house that has honestly labelled the three floors it hasn't built.

The good news is that they fail in *complementary* ways, and almost nothing worth keeping in either is in conflict with the other.

---

## 1. Method and evidence

| What | How verified |
|---|---|
| Bellona build status | `cargo check --workspace --all-targets` — **fails**, 9 errors, `manus` lib and lib-test |
| Achilles test status | `wsl … .venv/bin/python -m pytest tests/ -q` — **255 passed**, 2 warnings, 130.00s |
| Workspace escape | Standalone `rustc` reproduction of `resolve_in_workspace` verbatim — **escape confirmed** (output below) |
| Dead code / wiring | `grep` across all crates for each public symbol's consumers |
| Everything else | Direct reading of source, not documentation |

I read the code, not the docs. Where a doc comment and the code disagree, I cite the code.

---

## 2. Bellona — findings

### 2.1 CRITICAL — the workspace fence has a hole, and every file tool goes through it

[`bellona/src/lib.rs:64-80`](e:/bellona/bellona/src/lib.rs) — `resolve_in_workspace` canonicalizes and checks containment **only when the path already exists**. For a non-existent target it walks up to the deepest existing ancestor and **returns early, before the containment check ever runs**:

```rust
Err(_) => {
    let mut anc = full.as_path();
    while let Some(parent) = anc.parent() {
        if parent.exists() {
            let pc = parent.canonicalize()?;
            let tail = full.strip_prefix(parent).unwrap_or(&full).to_path_buf();
            return Ok(pc.join(tail));   // <-- line 72: returns; line 81's check never runs
        }
        anc = parent;
    }
```

Reproduced verbatim with `rustc`:

```text
workspace = ...\Temp\bellona_escape_probe/workspace
notes.txt                                 -> ...\workspace\notes.txt              INSIDE=true
../../pwned.txt                           -> \\?\C:\Users\priya\...\Temp\pwned.txt INSIDE=false
../../../../../../Windows/Temp/pwned.txt  -> \\?\C:\Users\Windows\Temp\pwned.txt   INSIDE=false
```

`WriteFileTool::execute` then calls `create_dir_all(parent)` and `fs::write`. **A model that emits `{"tool":"write_file","args":{"path":"../../../x/y.ps1"}}` writes outside the workspace, anywhere the process can reach.** Creating a new file is exactly the case the check skips, so the hole is open for precisely the operation that matters most.

The gate does not stop it either — see 2.4.

**Fix:** compute the candidate, normalize lexically (`..` removal) *before* touching the filesystem, canonicalize the deepest existing ancestor, and re-check containment on the joined result on *every* path, with no early return.

### 2.2 CRITICAL — the workspace does not build

```text
error[E0599]: no variant named `UiaInvoke` found for enum `ticket::ManusOp`     (×4)
error[E0599]: no variant named `FocusElement` found for enum `ticket::ManusOp`  (×4)
error[E0631]: type mismatch in closure arguments
error: could not compile `manus` (lib) due to 9 previous errors
```

[`manus/src/ticket.rs:14`](e:/bellona/manus/src/ticket.rs) declares seven `ManusOp` variants; [`manus/src/ticket.rs`](e:/bellona/manus/src/ticket.rs)'s own `describe()` and [`manus/src/sendinput.rs:283,284,312`](e:/bellona/manus/src/sendinput.rs) match on two that do not exist. `manus` is a dependency of the `bellona` binary, so **nothing ships**.

Context from `git status`: `manus/`, `oculus/`, `vigilia/`, `bellona/src/desktop.rs`, `hooks.rs`, `doctor.rs`, `market.rs`, `daemon_cli.rs` and `castra/src/harden.rs` are all **untracked**. The entire Campaign XV/XVI surface — desktop control, hooks, the daemon, the market — exists only in an uncommitted, non-compiling working tree. The last commit is titled *"chore: test-stable helper"*; the newest and most ambitious third of the system has never been committed and has never built.

This is the most important fact about Bellona and it is not a code-quality nitpick: **a harness that does not compile has no benchmark score, no tokens-per-solved-task, no eval gate, and no users.** Everything in §2.3 onward is downstream of fixing this.

### 2.3 CRITICAL — Vexillum identity is self-attestation

[`praetorium/src/custos.rs:265-275`](e:/bellona/praetorium/src/custos.rs):

```rust
if svc.agent_public(&req.agent_id.to_string()).is_none() {
    svc.ensure_owner();                          // mints an OWNER key if absent
    svc.enroll_agent(&req.agent_id.to_string()); // mints an AGENT key
}
let digest = effect_digest(&signed_req);
let rec = svc.attest(&req.agent_id.to_string(), &digest)?;
```

`VexillumService::attest` ([`vexillum.rs`](e:/bellona/praetorium/src/vexillum.rs)) signs as the agent, then countersigns as the owner — **with both private keys held in the same in-process `HashMap`**. Three consequences:

1. **The owner countersignature carries no human in it.** Law V says "every event is signed by the agent and countersigned by its owner." The owner is a key the gateway generated for itself, one line earlier.
2. **First-seen agents self-enrol.** Any agent id that reaches the gate gets a fresh valid standard on the spot. There is no enrolment authority to forge.
3. **`verify_export` never checks a trust root.** [`verify.rs`](e:/bellona/praetorium/src/verify.rs) verifies each `IdentityRecord` against the `agent_pub`/`owner_pub` *carried inside the record itself*. Any party can generate a keypair, sign an arbitrary ledger, and `bellona verify` prints `VALID ✔`. The receipt proves internal consistency, not provenance.

Keys are also never persisted (`HashMap<String, VexillumKeypair>`), so identity is destroyed on restart and yesterday's receipts cannot be tied to today's agent.

**Bellona's headline differentiator — "verifiable agency, third-party audit" — does not currently verify anything an adversary could not fabricate.** The cryptography is correct; the trust model is missing. Fix: a persisted owner key with a published fingerprint, out-of-process owner signing (the gateway holds only the agent key), and `verify_export` taking an expected owner pubkey as a required argument.

### 2.4 HIGH — the gate authorizes a URI; the executor is not bound to it

This is the architectural flaw the other findings hang from.

[`bellum/src/lib.rs:94-101`](e:/bellona/bellum/src/lib.rs) derives the policy target from **model-supplied arguments**:

```rust
req.target_uri = { let u = target_uri_hint(&call.args); if u.is_empty() { "file://workspace".to_string() } else { u } };
```

`Custos` resolves that string, evaluates policy against it, writes an audit row naming it — and then calls `EffectExecutor::perform(req, resolved, workspace)`, where `RegistryExecutor` ([`lib.rs`](e:/bellona/bellona/src/lib.rs)) passes `req.params` straight to the tool and **ignores `resolved` entirely** (`_resolved: &ResourceInfo`).

So: policy decides about `file://workspace/../../x`, the ledger records `file://workspace`, and the tool writes to `C:\x`. Authorization and execution are two independent facts joined by convention. This is exactly the gap ActPlane's empirical study measures — tool-boundary guardrails score *near zero* on indirect execution paths — and it is why the fix is not "harden the tools" but "make the authorized capability the only thing the executor can act on" (hand the tool a resolved, pre-opened handle, not a string).

### 2.5 HIGH — resolver returns first prefix match, not longest

[`praetorium/src/custos.rs:57-66`](e:/bellona/praetorium/src/custos.rs) iterates a `BTreeMap` and returns on the **first** prefix match. Register `file://workspace` (kind `workspace`) and `file://workspace/secrets` (kind `secret`) and a request for `file://workspace/secrets/id_rsa` matches `file://workspace` first — lexicographically shorter sorts earlier — and resolves to `kind = "workspace"`.

That silently defeats [`lex.rs`](e:/bellona/praetorium/src/lex.rs)'s `auriga_no_secrets` rule (`attr.resource.kind == 'secret'`), which is the *only* secret protection in the Auriga preset. Longest-match is mandatory for any prefix-scoped authorization table.

### 2.6 HIGH — approval re-executes without re-deciding

[`praetorium/src/custos.rs:334`](e:/bellona/praetorium/src/custos.rs) `approve()` removes the parked ticket, records `approval_granted`, re-resolves, and executes. It never re-runs `lex.decide()`. Since `install_law()` can swap the law at any time, a ticket parked under a permissive law and approved after a tightening executes under the old verdict.

Worse, `approver: &str` is an **unauthenticated string**. The "human countersignature" that Frenum mode is built on is a log field. Combined with 2.3, there is no point in the system where a human cryptographically assents to anything.

And `WarLoop::with_auto_approver` ([`bellum/src/lib.rs`](e:/bellona/bellum/src/lib.rs)) turns every `RequireApproval` into an immediate self-approval — which `--yolo` sets. Frenum's entire doctrine is one CLI flag away from being a no-op.

### 2.7 HIGH — the SSRF redirect shield never runs

[`bellona/src/lib.rs:410`](e:/bellona/bellona/src/lib.rs) builds the fetcher with `reqwest::Client::new()`. reqwest's default redirect policy is `Policy::limited(10)` — **the client follows redirects itself**. The hand-written loop in `web_fetch_tool` that re-checks `ssrf_check` on every hop only fires when `resp.status().is_redirection()`, which by then essentially never happens.

Net effect: `http://attacker.example/x` → 302 → `http://169.254.169.254/…` is followed transparently, and the shield the tool's own description advertises ("every redirect re-checked against the private-space shield") is dead code. Fix: `ClientBuilder::redirect(redirect::Policy::none())`.

Separately, [`arsenal.rs:341`](e:/bellona/bellona/src/arsenal.rs) strips ports backwards:

```rust
let bare = bare.rsplit(':').next().unwrap_or(bare);
```

`rsplit` yields right-to-left, so `example.com:8080` becomes **`8080`**, not `example.com`. Any URL with an explicit port is refused with a bogus DNS error, and the IPv6 path degrades similarly. The DNS-resolve-then-connect split is also textbook rebinding-vulnerable — resolve once and connect to the resolved IP with an explicit `Host`.

### 2.8 HIGH — the tamper-evident ledger is in memory

`CustosGateway::new` constructs `Annales::new()` — a `Vec<LedgerRecord>` ([`annales.rs`](e:/bellona/praetorium/src/annales.rs)). There is no WAL, no fsync, no persistence path. Law VII is "receipts or it didn't happen"; today the receipts do not survive the process. `merkle_root()` is implemented and never exported (`export()` emits `records` only).

`VetoGuard` also has `raise()` and no `lower()`. Once vetoed, the deployment is dead until restart — which destroys the ledger. The kill switch and the audit trail are mutually destructive.

### 2.9 MEDIUM — a third of the system is not wired to anything

Verified by grepping every consumer:

| Component | Claimed | Actual |
|---|---|---|
| `forge::ContextWindow` (one of the "seven primitives") | lineage-aware compaction, pinned survival | **referenced only from `memoria/tests/memory.rs`.** No loop, no binary, no crate uses it. `ReActStrategy` sends the model *goal + last observation only* — there is no conversation history and therefore nothing to compact |
| `forge_plugins` (WASM host) | "capability-scoped, deny-by-default, signed bundles" | **orphaned workspace member.** Nothing depends on it, including the binary |
| `castra` sandbox ladder | Prima → Secunda → Tertia → Quarta | **only `ProcessDriver` (Prima) exists.** And `validate_policy` refuses Prima for any policy needing writes or network — so every policy that needs anything has no driver at all |
| `foedus::mcp` / `foedus::acp` | "Bellona speaks both directions" | **traits with zero implementations.** `PROTOCOL_VERSIONS` advertises `mcp/2025-11-25` — two revisions stale; the 2026-07-28 spec is stateless with breaking changes |
| `windows_job::JobHandle` | "children bound to a kill-on-close job" | job is created; **nothing is ever assigned to it** — the code's own comment admits `AssignProcessToJobObject` "lives in user32-linked code paths this crate does not carry." No `Drop`, so the handle leaks too |

### 2.10 MEDIUM — the ReAct strategy never tells the model what tools exist

[`bellum/src/react.rs:82-95`](e:/bellona/bellum/src/react.rs) builds the prompt from goal + last observation + step count + a JSON format instruction. `ToolRegistry::exposed_specs()` exists and is never called by any strategy — `ReActStrategy` has no reference to the registry at all.

The model must guess tool names and argument shapes. With a local 7-9B model at `--base-url localhost:11434`, this cannot work. Contrast [`tools/dispatcher.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/dispatcher.py)'s `describe()`, which renders one line per tool with a concrete worked example specifically because "a 9B model copying a concrete example is dramatically more reliable than one inferring a schema."

Also: only `reply.tool_calls.first()` is honoured — no parallel tool calls, ever.

### 2.11 MEDIUM — the default law denies most of the shipped arsenal

[`bellona/src/lib.rs:320`](e:/bellona/bellona/src/lib.rs) `law()` ships three rules: deny shell, gate `file_write`, allow `file_read || list_files`. `assemble()` exposes **24 tools**. Everything whose effect is not `file_read`/`list_files`/`file_write` — `web_fetch` (`BrowserNavigate`), `search_files` (`Custom("search_files")`), and the entire Campaign XV desktop arsenal (`oculus_observe`, all six `manus_*`, `word_command`, `excel_command`, the browser and window tools) — falls through to `__lex_default_deny__`.

So the flagship desktop-control capability is unreachable under the default law, and the flag that unlocks it (`--drive-mode auriga`) **is not in `--help`**.

`--allow-shell` is also inert on its own: with `yolo=false, allow_shell=true` the deny expression becomes `run_shell && !true` (never fires) and neither allow branch is taken (both are gated on `yolo`), so shell lands in default-deny. The flag documented as "permit run_shell" permits nothing.

### 2.12 MEDIUM — sandbox driver defects

[`castra/src/lib.rs:109-115`](e:/bellona/castra/src/lib.rs):

```rust
fn now_limited(s: String, cap: usize) -> String {
    if s.len() > cap { format!("{}…[truncated]", &s[..cap]) } else { s }
}
```

`&s[..16_384]` **panics** when byte 16384 is not a UTF-8 boundary. `from_utf8_lossy` output routinely contains multi-byte characters; any command producing >16 KiB of non-ASCII output crashes the run. Use `char_indices` or `floor_char_boundary`.

[`castra/src/lib.rs:139`](e:/bellona/castra/src/lib.rs): `tokio::time::timeout(…, command.output())` — on timeout the future is dropped, but tokio's `Command` does **not** kill on drop by default. "camp timeout" leaves a live orphan process holding the workspace. Set `kill_on_drop(true)`, and on Windows bind to the (currently unused) job object.

### 2.13 LOW — assorted

- **Legion silently truncates.** [`centurio.rs:88`](e:/bellona/bellum/src/centurio.rs) `take = plan.len().min(self.max_workers)` — workers beyond 8 are dropped with no error and no mention in `FleetReport`. It is also strictly **sequential** despite the "one plan, many specialists" framing, and the synthesis model call is outside every budget.
- **Drive mode is not audited.** [`primitives.rs`](e:/bellona/forge/src/primitives.rs) claims the mode is "recorded in audit rows AT EXECUTION TIME, so a mid-campaign flip is provable after the fact." The `decision` payload at [`custos.rs:282`](e:/bellona/praetorium/src/custos.rs) contains no `drive_mode` field, and `effect_digest` does not commit to it. The claim is false in both directions.
- **WASM host has no fuel, epoch, or memory limits.** [`forge_plugins/src/lib.rs:104`](e:/bellona/forge_plugins/src/lib.rs) `Engine::default()`; an infinite loop in a plugin hangs the host forever. Line 179 allocates `vec![0u8; out_len]` from a **guest-controlled** return value (up to `i32::MAX`). And "signed bundles" means a SHA-256 that the manifest declares about itself — self-referential, no publisher key, no trust root; `ed25519-dalek` isn't even a dependency of the crate.
- **Somnium is deduplication, not consolidation.** [`somnium.rs`](e:/bellona/memoria/src/somnium.rs) counts exact-duplicate episode contents and calls anything appearing twice a "skill." Each pass reloads every episode of every kind plus all prior distillates — O(n²) over the life of the archive, unbounded.
- **`HashEmbedder` is a bag of hashed tokens**, explicitly "not semantic." Vector recall is keyword recall wearing a cosine.
- **Encoding corruption.** `custos.rs`, `bellum/src/lib.rs`, `main.rs` and others carry triply-mis-encoded doc comments (`ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢…`), and five `Cargo.toml` files start with a UTF-8 BOM. Cosmetic, but it is visible in `--help` output, and it signals a toolchain that has been mangling files repeatedly without anyone reading them.

### 2.14 What Bellona gets genuinely right

Being unsparing cuts both ways — this list is real:

- **One mandatory path from decision to effect.** `submit()` → resolve → decide → audit → execute → settle, fail-closed at every stage, audit row written *before* execution. Most harnesses do not have a single choke point at all.
- **Structural deny-before-allow.** [`lex.rs`](e:/bellona/praetorium/src/lex.rs) groups rules into `denies`/`approvals`/`allows` vectors so ordering is a property of the data structure, not of the config file. Achilles cannot express a deny at all. This is the better design, full stop.
- **A broken rule denies.** CEL evaluation error → `Deny{RULE_BROKEN}`; no match → `Deny{RULE_DEFAULT_DENY}`; refusals name their rule id. Exactly right.
- **Registration ≠ exposure** ([`forge/src/tool.rs`](e:/bellona/forge/src/tool.rs)) — borrowed from Hermes, credited, and correctly implemented.
- **The Ludus** ([`officina/src/ludus.rs`](e:/bellona/officina/src/ludus.rs)): a self-forged tool cannot enter the registry until it survives a battery *and* carries a countersignature. This is the "skill library with a maintenance cycle" gap from research.md §6.D, and Bellona is the only one of the two that has the idea at all.
- **Colosseum pass^k with a cost gate** ([`vigiles/src/colosseum.rs`](e:/bellona/vigiles/src/colosseum.rs)): a case passes only if **all k** trials pass, and the gate fails on budget as well as reliability, with distinct exit codes. That is the τ-bench doctrine plus the cost axis research.md §2.1 says nobody reports.
- **Aerarium as a breaker**, not a report: over-budget halts the run and an over-budget answer is recorded as evidence, not success.
- **Manus ticket design**: ed25519-signed, gate-issued action id, freshness window, seen-ids refused forever, loopback-only transport. The *shape* is right even though the enum doesn't compile.

---

## 3. Achilles — findings

### 3.1 HIGH — the policy engine cannot say "no"

[`kernel/policy.py`](d:/local-sovereign-ai/src/sovereign_ai/kernel/policy.py) matches rules on exactly two fields — `action` and `scope` — by string equality, first match wins, and **there is no deny rule type**. A rule can only be `allowed=True` with optional `approval`. The only refusals available are the untrusted-content gate and the fail-closed default.

That means you cannot express any exception to a grant. `write:workspace` is one undivided authority — there is no way to say "write to the workspace but never to `.env`, `.git/hooks`, or `*.ps1`". No target/path attribute reaches the engine at all: `ActionRequest` carries `description` (free text, unmatched) and nothing structured about *what* is being written.

Against research.md §4.10's finding that **73.6% of real agent policies need project or task context**, this engine can express roughly none of them. Bellona's CEL rules over a nested attribute tree are a generation ahead here, and Achilles should simply take them.

### 3.2 HIGH — a capability grant switches the untrusted gate off for its whole scope and TTL

[`execution/broker.py`](d:/local-sovereign-ai/src/sovereign_ai/execution/broker.py) `authorize()` checks grants **before** policy and returns early:

```python
grant_authorized = bool(subject_id and self.capability_grants.is_active(subject_id, action, scope))
if grant_authorized:
    return PolicyDecision(allowed=True, approval_required=False, …)
```

The reasoning in the comment is sound as far as it goes — the untrusted gate can never return `allowed` for `execute`, so without this a grant could never authorize anything. But the consequence is not stated: once a human issues `execute:workspace` for 300 seconds, **every** model-proposed execution in that scope runs unreviewed for 300 seconds, and the crown-jewel property is off for the duration.

The grant is bound to `(subject, action, scope)`. It should be bound to the **specific action instance** — an argv digest, or a target path — so that approving one command approves one command. `scripts/harness_tournament.py` issues exactly these blanket 300s grants for its runs, which is fine for a benchmark and is precisely the pattern that must not become the default.

### 3.3 MEDIUM — declared capabilities with no implementation behind them

To Achilles's credit the README says most of this out loud. It is still a gap:

- `ComputerController` ([`computer/controller.py`](d:/local-sovereign-ai/src/sovereign_ai/computer/controller.py)) is a clean six-tier abstraction (NATIVE_API → CLI → PLUGIN → DOM → ACCESSIBILITY → VISION_GUI) with **zero registered controllers**. `execute()` always raises. There is no browser or desktop control.
- **7 of 14 specialist workers return HTTP 501.**
- SearXNG is deployed by the installer and **nothing in the kernel queries it**.
- `WorkspaceLease` enforcement is opt-in and, by its own docstring, **no current caller passes it** — dead code by design decision, deferred at F-031.
- Nothing streams.

The tiering abstraction is the right one (structured control first, pixels last — this is what OSWorld leaders do). But an interface with no implementations is a plan, and it is currently indistinguishable in the docs from a feature.

### 3.4 MEDIUM — compaction drops the middle and counts it

[`agents/context.py`](d:/local-sovereign-ai/src/sovereign_ai/agents/context.py) `compact_history` keeps 1 leading + 6 recent turns and replaces everything between with `{"elided_turns": n, "tools": {...}}`.

The reasoning is excellent and I agree with it — deterministic elision "costs nothing and cannot lie," where a summarizer costs a generation and can invent a step. But measured against research.md §4.5:

- There are no **typed, dependency-linked episodes**, so eviction cannot be priority-ordered — it is positional. A load-bearing decision made at turn 3 of a 30-turn run is dropped with the same indifference as a failed `ls`.
- There is **no invariant set**. Nothing is un-evictable. This is the governance-decay failure mode exactly: a constraint stated mid-run vanishes silently.
- `max_history_chars` (24,000) is checked to *decide* whether to compact, but **never enforced after eliding**. Seven surviving turns containing large observations still blow the budget, and nothing catches it.
- There is no **validation pass** confirming the compaction preserved anything.

### 3.5 MEDIUM — file mutations never reach the hardened execution backend

`ExecutionBroker.run_approved` fails closed to OpenShell → Docker → `RuntimeError("No hardened execution backend available")`. That is correct and good. But it only covers `run_command`. `write_file`, `edit_file` and `delete_file` ([`tools/files.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/files.py)) authorize through `broker.authorize()` and then perform **in-process Python filesystem writes** — no container, no isolation, no lease.

`DiffSandbox`'s docstring is honest about this ("a file-mutation sandbox, not a container"), and it genuinely mitigates it when enabled. But `--sandbox` is **opt-in and defaults to `False`**, so the default path writes directly with only a policy check standing in front.

### 3.6 MEDIUM — maintainability and surface

- **One 5,893-line test file** holds all 255 tests. They pass, they're real, and that is a genuine achievement — but the monolith will become the bottleneck, and there are no property or fuzz tests anywhere near the path-resolution and policy code, which is where both codebases' worst bugs live.
- **22 SQLite databases** in `state/`. There is a `kernel/migrations.py`, so this isn't unmanaged — but 22 separate stores means 22 separate consistency stories, and no single ordered log that a replay or an audit can read.
- The web UI is one `index.html`; the Tauri app is self-described as "a first vertical slice."

### 3.7 What Achilles gets genuinely right

- **Trust labels, enforced end to end.** Every mutating tool passes `trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT` — verified across [`files.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/files.py), [`capabilities.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/capabilities.py), [`shell.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/shell.py), [`mcp_client.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/mcp_client.py) — and `PolicyEngine` refuses untrusted-origin mutation and credential access unconditionally. Web results are `UNTRUSTED_WEB`; MCP output is `UNTRUSTED_DOCUMENT`; **`AGENTS.md` is loaded as untrusted document content** ("guidance, not authority... can shape how work is done and can never widen what may be done"). This is the dual-LLM/CaMeL boundary, built correctly, in a shipping codebase. It is the most valuable single asset across both repositories.
- **`DiffSandbox`** ([`kernel/sandbox.py`](d:/local-sovereign-ai/src/sovereign_ai/kernel/sandbox.py)). Writing into the sandbox needs no authority because nothing real changes; **applying** does. It converts "approve a class of writes in advance and never see them" into "read this diff, then commit." Overlay reads work, so the agent can build on its own pending edits. `sovereign diff` / `apply` / `discard` are real commands. This directly answers a documented 2026 open problem and nothing in research.md §3 ships it.
- **The advisor's asymmetry.** [`native_loop.py`](d:/local-sovereign-ai/src/sovereign_ai/agents/native_loop.py): a second model reviews each action and may **block**, but "an objection, not an authorisation... nothing here can ever let an action through that policy would refuse." Getting that direction right is the whole ballgame and most multi-agent review designs get it wrong.
- **The action schema is derived from the registry**, so constrained decoding cannot drift from the tool plane ([`dispatcher.py`](d:/local-sovereign-ai/src/sovereign_ai/tools/dispatcher.py) `action_schema`). Constraint failure degrades to prose parsing **and records the degradation as an event** rather than silently.
- **Mutation-aware batching**: read-only batches run concurrently via `asyncio.gather`; any mutating call forces sequential execution in model-requested order, "or it scrambles the order of the audit events and shadow-git checkpoints." One event per call either way. That is the correct reasoning about why concurrency is unsafe here.
- **The harness tournament** ([`scripts/harness_tournament.py`](d:/local-sovereign-ai/scripts/harness_tournament.py)): replay identical tasks through every registered `AgentLoop`, score post-conditions, unsafe attempts, recovery, tokens, wall time and operator interventions — with `native`, `goose`, `opencode` and `pi` loops as pluggable adapters, and an explicit rule that it "produces *evidence*, never a promotion." **This is the closest thing either repository has to a world-first**, and it is exactly the instrument research.md §2.1 says the field lacks.
- **Honest self-documentation.** The README's "What is implemented" section leads with what is *not* there. `IMPLEMENTATION_STATUS.md` exists "to prevent the architecture from being mistaken for capabilities." `FIXES.md` is 3,873 lines of tracked defects with evidence and severity. Bellona's `BELLONA.md` asserts seven Laws that the code violates in five places.
- **255/255 passing tests**, and a per-turn model routing split (`plan_role`/`act_role`) that is real hardware-aware engineering: 6.36 tok/s deep brain for turn zero, 49.57 tok/s fast brain for the mechanical turns after it.

---

## 4. Scorecard against research.md §4

0 = absent · 1 = declared/stubbed · 2 = works, well behind best · 3 = credible, ~half of best · 4 = near best-in-class · 5 = world-best

| Component | World-best (research.md) | Bellona | Achilles | Best of the two |
|---|---|---|---|---|
| **4.2 Agent loop / kernel** | Prime Agent RLM; DeepSeek loop-as-plugin | **2** — strategy trait is clean, but ReAct has no tool list, no history, no parallel calls | **3** — real JSON protocol, batching, constrained decode, role routing | Achilles |
| **4.3 Plugin architecture** | Cordis reversible effects | **1** — WASM host orphaned, no signatures, no fuel | **2** — in-process `HookRegistry`, MCP bridge; no lifecycle guarantees | Achilles (barely) |
| **4.4 Tool interface** | code mode / tool search | **2** — registration≠exposure is right; no code mode, no search, no progressive disclosure | **3** — worked-example rendering, contextual discovery, derived schema, batching | Achilles |
| **4.5 Context & compaction** | CWL typed eviction + validation | **0** — `ContextWindow` is dead code; the loop has no history at all | **2** — deterministic, honest elision note; positional, no invariants, budget unenforced after eliding | Achilles |
| **4.6 Memory & state** | Mem0 / LongMemEval | **2** — four tiers, SQLite archivum, hybrid RRF recall — but non-semantic embedder, O(n²) consolidation | **3** — lexical + vector + graph + provenance, 22 stores | Achilles |
| **4.7 Skills & self-learning** | SKILL.md + ACE | **2** — Ludus battery + countersigned promotion is the right *idea*; consolidation is dedup | **2** — `kernel/skills.py`, skill service | Tie (Bellona's idea, Achilles's rigor) |
| **4.8 Self-improving harness** | Prime Agent `/refine`, L0–L5 | **1** — self-forged tools only | **1** — tournament measures, never promotes | Neither |
| **4.9 Sandbox / runtime** | Antigravity native primitives; Firecracker | **1** — one rung, and it's the rung that can't write; job object unassigned | **3** — OpenShell/WSL2 + Docker, fail-closed, real | Achilles |
| **4.10 OS-level policy** | ActPlane eBPF/BPF-LSM | **1** — Job Object *intent*, unwired | **1** — OpenShell policy YAML, container-level | Neither — **this is the open lane** |
| **4.11 Security & permissions** | OAP + OPA + kernel + signed log | **3** — CEL, deny-first, hash chain, veto — undermined by 2.1/2.3/2.4/2.6 | **4** — trust labels are the real thing; policy language is weak | Achilles for enforcement, Bellona for expression |
| **4.12 Browser & computer use** | browser-use / Playwright MCP; OSWorld ~86% | **2** — oculus UIA read + manus signed input (doesn't compile) | **1** — six-tier interface, zero controllers | Bellona (once it builds) |
| **4.13 Codebase intelligence** | local-first graph + LSP | **1** — recursive grep, 200-hit cap | **2** — ranged reads, outline heuristic, glob/grep | Achilles |
| **4.14 Protocols** | MCP/ACP/A2A/AG-UI/SKILL.md | **2** — A2A real; MCP stdio partial; ACP traits only; version string 2 revisions stale | **2** — MCP client + bridge real; no ACP, no AG-UI | Tie |
| **4.15 UI/UX & surfaces** | OpenCode client/server; Codex App Server | **2** — CLI + war room + Telegram/Discord/Slack | **2** — Typer CLI with per-step printing, web page, Tauri slice | Tie |
| **4.16 Multiplayer** | Amoeba / Buzz / jcode | **1** — channels only | **3** — real rooms, identities, threads, canvases, mention→job, per-room hash chain | Achilles |
| **4.17 Orchestration & durability** | Temporal-class replay | **1** — Legion is sequential and truncates silently | **3** — durable job journal, per-attempt `Run`, cancellation, restart detection (no auto-resume) | Achilles |
| **4.18 Model layer** | Bifrost routing; local-first | **2** — `CascadeRouter`, OpenAI-compat + Anthropic | **4** — VRAM fitting, dual brains, idle sleep, GPU arbiter, benchmark DB, remote quota ledger | Achilles |
| **4.19 Observability & evals** | OTel GenAI + Langfuse; HAL | **3** — pass^k + cost gate + spans + replay | **4** — trust-labelled append-only events, harness tournament, quality eval scripts | Achilles |
| **4.20 Economics** | tokens/solved-task, no-action turns | **2** — Aerarium ceilings, cost in gate verdict | **1** — wall time and tok/s measured; no tokens-per-solved-task | Bellona |
| | **Mean** | **≈1.6** | **≈2.4** | |

Neither system scores 4 on more than two rows. The 80–90%-of-best-in-class target is roughly **a 4.0 mean**. That is the actual distance to travel.

---

## 5. The pattern behind Bellona's failures

Every one of Bellona's critical defects has the same shape: **a correct and often sophisticated mechanism, terminated one step before the thing it was protecting.**

- Containment check written correctly — and skipped on the branch that matters.
- ed25519 signing implemented correctly — over keys the signer mints for itself.
- Hash chain implemented correctly, with a Merkle root — held in a `Vec` and never exported.
- Job object created correctly with kill-on-close — with no process assigned.
- WASM capability denial implemented correctly — with no fuel limit, so a plugin just hangs.
- SSRF checks written thoroughly — bypassed by the HTTP client's default redirect policy.
- Deny-before-allow ordering made structural — over a resolver that returns the wrong resource kind.

That is not carelessness; it is the signature of building at high speed against a doctrine document and marking each Law "shipped" when its *shape* exists. `BELLONA.md` says "docs are tested." They aren't: 147 test functions exist, and none of them writes a `../..` path, restarts the process and re-verifies the ledger, or checks a signature against an out-of-band key.

**The single highest-leverage change to Bellona is not a feature. It is one adversarial test per Law**, written to break it rather than demonstrate it. Five of the seven Laws fall to a test that fits on one screen.

Achilles's pattern is the inverse and much healthier: it under-claims, tests what it builds, and writes down what it hasn't built. Its risk is different — **it will keep being correct about a smaller and smaller share of what a 2026 harness needs to do** unless the architecture (policy expressiveness, context discipline, self-improvement, OS-level enforcement) grows.

---

## 6. What Optimus takes, and what it leaves

### Take from Bellona (the ideas, almost none of the code)

1. **The single mandatory gate** — resolve → decide → audit → act → settle, with audit preceding execution. Keep the shape; fix the binding (§2.4).
2. **CEL policy with structural deny-before-allow**, broken-rule-denies, and rule ids in every refusal. Drop this straight into Achilles's engine to fix §3.1.
3. **Hash-chained ledger + Merkle root + a standalone `verify` command** — with a persisted, out-of-band owner key so it verifies provenance and not just internal consistency.
4. **Registration ≠ exposure.**
5. **The Ludus**: no self-forged tool enters the registry without passing a battery. This is the skill-maintenance cycle research.md §6.D says nobody ships.
6. **pass^k with a cost gate and distinct exit codes.**
7. **Aerarium as a breaker, not a report** — and extend it to the metric nobody publishes: tokens per solved task.
8. **The manus ticket shape** — gate-issued action id, freshness window, permanent replay memory, loopback-only.

### Take from Achilles (the code, largely as-is)

1. **Trust labels, enforced at every authorize site.** Non-negotiable; this is the foundation.
2. **`DiffSandbox`** — and make it the **default**, not `--sandbox`.
3. **The advisor asymmetry**: reviewers may block, never authorize.
4. **Schema derived from the tool registry** + recorded degradation on constraint failure.
5. **Mutation-aware batching** with one audit event per call.
6. **The harness tournament** — this is the seed of the unique thing. See §7.
7. **`AGENTS.md` as untrusted document content.**
8. **Honest status docs as a discipline**, including `FIXES.md`.
9. **Hardware-aware routing**: VRAM fitting, dual brains, idle sleep, GPU arbiter, plan/act role split.

### Leave behind

- Bellona's `forge_plugins` (rewrite against a real component model with fuel, epochs, memory limits and publisher signatures — or drop WASM entirely and use the OS boundary).
- Bellona's `HashEmbedder` and `HeuristicConsolidator`.
- Bellona's `foedus` MCP/ACP traits (write real clients against the **2026-07-28** spec; the stateless core is a breaking change and the current version string is already wrong).
- Bellona's `ContextWindow` (superseded by a typed-episode design).
- The 15-crate Latin taxonomy. It is genuinely charming and it costs real time: `castra`/`officina`/`vigiles`/`vigilia` are two keystrokes apart and mean unrelated things, and no contributor outside this repo can navigate it. Keep the names as *doc-comment* flavour; make the module paths say what they do.
- Achilles's 22 independent SQLite stores as the system of record — see §7.

---

## 7. Where the unique thing actually is

research.md §7 offered eight sparks. Pass 2 kills some and sharpens others.

**Dead on arrival.** *Fork-the-world speculative execution* — neither system has a snapshot primitive, and building one on Windows means Hyper-V or WSL2 checkpointing before any agent work starts. Wrong first fight.

**Confirmed and cheap, because half of each already exists:**

- **The Ledger.** Bellona has a hash chain with no persistence; Achilles has durable, trust-labelled, append-only events with no chain and no signatures, spread over 22 databases. **One signed, hash-chained, trust-labelled append-only log as the sole system of record** — with memory, compaction, audit, replay, undo and multiplayer coordination as *projections* over it — is a merge of two things that already work, not new research. It also fixes §2.8 and §3.6 at once.
- **The immune system.** Achilles's tournament already replays fixed tasks and scores post-conditions, unsafe attempts and interventions, and already refuses to promote on its own results. Bellona's Ludus already refuses promotion without a passing battery. Wire them together and you have what research.md §4.8 says nobody ships: **self-modification that must pass a frozen external verifier, with a human-readable diff and automatic revert.** Prime Agent has rollback; it has no independent verifier. This is buildable in weeks from parts that exist.

**The strongest claim, and the one to commit to:**

> **Optimus should be the harness whose own trustworthiness is a measured, published, regression-tested quantity — on Windows, at the OS boundary.**

Three legs, all of which are gaps in the *field*, not just gaps here:

1. **Kernel-enforced, agent-authored policy on Windows.** ActPlane proved the model on Linux (eBPF + BPF-LSM, 1.9% overhead, 2–3.2× over tool-layer guardrails, near-zero baseline detection on indirect execution paths, 83% of real policies needing OS-level enforcement). **Nobody has published the Windows equivalent.** The primitives exist — ETW, Windows Filtering Platform, minifilter drivers, AppContainer, Job Objects, WSL2 — and Bellona already reaches into `windows-rs` for UIA and Job Objects while Achilles already owns the Windows→WSL2 bridge. This is the one place where "first in the world" is literally available.
2. **Compaction that provably preserves invariants.** Governance decay is documented, measured, and unaddressed by every shipped harness. Achilles's deterministic elision is the right substrate; it needs typed episodes, a never-evictable invariant set, and a post-compaction validation pass. Small work, unclaimed ground.
3. **Cost as a headline capability.** research.md §2.1: 40× token spread at equal pass rate, and nobody optimizes for it deliberately. Bellona already treats budget as a breaker and already fails an eval gate on spend. Publishing tokens-per-solved-task and no-action-turns-per-task alongside pass^k, as a regression-tested gate, is a defensible world-first claim that costs almost nothing to build — and it is what makes local models genuinely competitive, which is the entire point of both projects.

None of that is reachable while the Rust workspace does not compile and the file fence has a hole in it. **Order of operations: fix §2.1 and §2.2 first, write the seven adversarial Law tests, then merge.** Everything else in this document is a plan; those are the floor.

---

*Pass 2 complete. Pass 3 should be the merge architecture: which runtime hosts the kernel, how the trust-label plane and the CEL gate compose into one authorization path, and the concrete Windows OS-enforcement spike.*

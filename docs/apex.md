# Optimus — The Apex Harness

The whole-system architecture. Supersedes [architecture.md](architecture.md), which is demoted to the specification for **one plane** (OS/desktop) attached late.

Reads on top of [research.md](research.md) (the field, Aug 2026) and [audit.md](audit.md) (what the two prior systems taught us).

---

## 0. What pass 3 got wrong

You're right. `architecture.md` optimized one row of [research.md](research.md) §4 — computer use — and let it eat the design. That is a **greedy** move: it took the highest-variance differentiator and built the system around it, which maximizes one cell and leaves eighteen others at 1–2/5.

This document does the other thing. It treats component selection as an optimization **over the whole table at once, subject to compatibility constraints**, and it puts the OS plane back where it belongs: a plane, mounted at M6, on a system that is already 4.5/5 without it.

---

## 1. The optimization, stated properly

### 1.1 Why greedy fails here

Greedy = for each row of research.md §4, take the world-best component. Do that literally and you get:

> Prime Agent's RLM loop **+** Cordis's reversible plugin kernel **+** Goose's token frugality **+** Mem0's memory **+** OpenCode's client/server TUI **+** UFO2's desktop control **+** verifiers' eval decomposition.

That set is **infeasible**, and not for taste reasons:

- **Prime Agent is TypeScript** (verified — a TS monorepo that drives a *Python* IPython kernel; MIT; CLI + JSON + RPC + daemon). **Cordis is TypeScript.** Forking either forces a Node spine.
- A Node spine makes every deep Python integration — UFO2, Windows-MCP, browser-use, Mem0, verifiers/prime-rl, Achilles's tested trust plane — subprocess-only. The ones that are *tool servers* survive that fine. **UFO2 does not**: it is a full agent with per-app AppAgents and a control-detection pipeline we want as a *library*, not as a black box behind a pipe.
- **Goose's frugality and progressive disclosure are contradictory strategies.** Goose gets 28–37K tokens/solved-task by *eagerly pre-injecting the file tree*; tool-search/code-mode gets its win by *deferring* everything. You cannot install both as-is; you have to design the policy that chooses between them.
- **Two "best" components can implement the same row.** OpenHands SDK ships event-sourced state, and a Ledger is also event-sourced state. Adopting both means two systems of record — strictly worse than either.

Greedy scores high on paper and does not compose. This is exactly the knapsack-with-dependencies shape where per-item value is the wrong objective.

### 1.2 The DP formulation

**State** = the four decisions everything else hangs off:

```
S = (runtime, tool-paradigm, extension-boundary, system-of-record)
```

**Transition cost** = for each row, the best achievable score *given* S. Most rows are neutral to S; a handful are forced by it.

**Objective** = maximize Σ(row scores) subject to feasibility, **not** maximize any single row.

The whole problem collapses because two of the three spine-forcing constraints dissolve under the right choice — which is the non-obvious path a greedy walk never finds:

**Dissolution 1 — code-mode does not require a TS harness.**
Prime Agent's own architecture proves it: the harness is TypeScript, the model's only tool is a persistent **Python** kernel. Harness language and execution-plane language are already decoupled *in the best-in-class implementation*. So we can build RLM-shaped code-mode as a Tool in a Python harness — where the kernel is native rather than foreign. We lose the fork; we keep the capability, more cheaply.

**Dissolution 2 — plugin reversibility does not require Cordis.**
Cordis's contribution is *temporal composability*: unmounting a plugin provably reverts its listeners, connections and shared-state mutations. If extensions are **processes speaking MCP** rather than in-process objects, **the OS provides that guarantee for free** — process death *is* the effect boundary, and anything that escaped it (files, registry, network) is caught by the compensation journal instead. We give up in-process hot-reload elegance. We get the entire Python ecosystem and a stronger isolation story. That is a good trade and it is the pivot of this whole design.

With both dissolved, the optimal spine is forced:

```
runtime           = Python
tool-paradigm     = hybrid, budget-selected (schema | search | code-mode)
extension-boundary= process + MCP  (in-process only for the hot path)
system-of-record  = one event-sourced, signed, hash-chained Ledger
```

### 1.3 The core that falls out

**Fork [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk) as the substrate.** MIT, Python, explicitly *an embeddable SDK rather than a framework*. It arrives with the exact sockets our differentiators plug into:

| It already has | Which row that is | What we do with it |
|---|---|---|
| **Pluggable security analyzers** — risk assessment, confirmation policy, action validation *before execution*, audit trail in event history | Security & permissions | **This is the Gate's socket.** We implement one analyzer: trust labels + CEL + handles |
| **Event-sourced state + deterministic replay** | State / durability | **This is the Ledger's substrate.** We add signing, hash-chaining, trust labels, projections |
| **MCP-native tools** | Tool plane | Mount everything; our gateway namespaces and budgets it |
| **Model-agnostic routing, 100+ providers** | Model layer | Keep; add Achilles's local VRAM-fitting/dual-brain layer beneath it |
| **Optional sandboxing + remote execution** | Sandbox | Keep; add venues (Agent Workspace, WSL2) as backends |
| **REST/WebSocket server** | Surfaces | Keep; this is the protocol-first core opencode and Codex both converged on |
| **Context condensation** | Context | **Replace** — see §4, this is where we pay a debt |
| Already a registered agent in **Harbor** | Evals | Benchmark parity costs one adapter class |

We are not adopting OpenHands' *agent*. We are adopting its *abstractions* — Agent, Conversation, Event, Tool, LLM, SecurityAnalyzer — and putting our loop, context discipline, gate and ledger on top. That is precisely the composability it advertises.

---

## 2. The Apex bill of materials

Every row of [research.md](research.md) §4. **Mount** = we don't write it. **Adapt** = thin wrapper. **Build** = ours.

| # | Row | World-best (research.md) | Apex pick | Mode | Target |
|---|---|---|---|---|---|
| 1 | **Agent loop / kernel** | Prime Agent RLM | OpenHands `Agent` + **our loop**: code-mode default, sub-agents as async calls, speculative batching | build on adapt | 4.5 |
| 2 | **Plugin / extension** | Cordis reversible | **Process+MCP boundary** + compensation journal; in-process only for hot path | build (thin) | 4 |
| 3 | **Tool interface** | code-mode / tool search | **Three-mode budget policy** over an MCP gateway that generates typed façades | build on mount | 5 |
| 4 | **Context & compaction** | CWL typed eviction, CompactionRL | **Typed dependency-linked episodes + never-evictable invariant set + post-compaction validation** | build | 5 |
| 5 | **Memory & state** | Mem0 (token-frugal), LongMemEval | **Mem0** mounted, backed by Ledger projections, with provenance + confidence on every recall | mount + build | 4.5 |
| 6 | **Skills & self-learning** | SKILL.md + ACE | **SKILL.md conformance** + Bellona's Ludus battery as the promotion gate + ACE-style delta curation | build on standard | 4.5 |
| 7 | **Self-improving harness** | Prime Agent `/refine` (L0–L3) | **Candidate → frozen battery → reviewable diff → one-command revert → demote on drift** | build | 5 |
| 8 | **Sandbox / venues** | Antigravity native primitives; Firecracker | OpenHands sandbox + **venue abstraction**: local · WSL2 · Docker · Agent Workspace · VM | adapt | 4 |
| 9 | **OS-level policy** | ActPlane (eBPF/BPF-LSM) | Linux: eBPF spike. **Windows: ETW + WFP + AppContainer + Job Objects** | build (late) | 3→5 |
| 10 | **Security & permissions** | OAP + OPA + kernel + signed log | **The Gate**: trust labels (Achilles) + CEL deny-first (Bellona) + **handles, not verdicts** + instance-bound grants | build | 5 |
| 11 | **Browser & computer use** | browser-use / CDP MCP; OSWorld 86% | **Chrome DevTools MCP** + Playwright MCP; browser-use for agentic mode | mount | 4 |
| 12 | **Codebase intelligence** | local-first graph + LSP | **open-codebase-index** (Rust+tree-sitter, speaks MCP) + LSP | mount | 4.5 |
| 13 | **Protocols** | MCP · ACP · A2A · AG-UI · SKILL.md | All five. MCP client+server, ACP client, AG-UI emitter, SKILL.md native | adapt | 5 |
| 14 | **UI/UX & surfaces** | OpenCode client/server; Codex App Server | OpenHands REST/WS core + **TUI, web, ACP, watch pane**; priority-queue steering; confidence signalling; pre-flight dry-run | build on adapt | 4.5 |
| 15 | **Multiplayer** | Amoeba · Buzz · jcode | **Read-set/write-set tracking + mutation bus + signed provenance** — one mechanism, four features | build | 4 |
| 16 | **Orchestration & durability** | Temporal-class replay | OpenHands deterministic replay + **Temporal SDK** for long-running; worktree venues | mount + adapt | 4 |
| 17 | **Model layer** | Bifrost; local-first open weights | OpenHands routing + **Achilles's VRAM fitting, dual brains, idle sleep, GPU arbiter, plan/act split** | port | 4.5 |
| 18 | **Observability & evals** | OTel GenAI + Langfuse; HAL | **OTel spans → Langfuse** + **Harbor** for Terminal-Bench/SWE-bench + Ludus batteries | mount | 5 |
| 19 | **Economics** | *nobody* | **tokens-per-solved-task + no-action-turns as gated, published metrics** | build | 5 |
| 20 | **OS/desktop plane** | UFO2 + Windows Agent Workspace | [architecture.md](architecture.md), attached at M6 | mount | 4 |

**Six things to build: the Gate, the Ledger + compensation journal, the Context plane, the Skill/Descent plane, the Economy, the Watch/steering surface.** Everything else is mount, adapt or port. That is the DP result — and it is *fewer* net-new subsystems than the greedy plan, because compatible components do more work per unit of integration.

---

## 3. Composition invariants

Five rules. If a component violates one, it doesn't get mounted — this is what makes the assembly a system instead of a bag of parts.

1. **One authorization path.** Every effect — file write, PowerShell line, MCP call, UIA click, model call — mints a `CapabilityRequest` and receives a **handle** carrying a *resolved* target (open fd, pinned element, validated argv), never a string the executor re-parses. Executors accept nothing but handles. This structurally removes the bug class that produced Bellona's arbitrary-write escape ([audit.md](audit.md) §2.1, §2.4).
2. **One system of record.** The signed, hash-chained, trust-labelled event log. Memory, sessions, audit, replay, undo, cost accounting, multiplayer views are **projections**. No component may hold authoritative state the Ledger doesn't have.
3. **Trust is provenance, and it never widens.** Untrusted-origin content (model output, web, documents, MCP results, `AGENTS.md`, collaboration) can shape *how* work is done and can never widen *what may be done*. Enforced at every authorize site, not at the loop.
4. **Reversibility is a declared type.** Overlay-reversible · compensation-reversible · snapshot-reversible · irreversible. Policy keys on the type. **Irreversible always requires human assent showing the actual payload.** The inverse is written to the Ledger *before* the act.
5. **Every capability is metered.** Tokens, wall time, no-action turns and cost attach to every action and aggregate per solved task. A component that cannot be metered cannot be promoted.

---

## 4. The one debt we take on, and how we pay it

Adopting OpenHands SDK costs us on the row we most want to win. From the Scaffold Effect study ([research.md](research.md) §2.1), on 50 Terminal-Bench Pro tasks:

| Harness | Tokens / solved task | No-action turns / task |
|---|---|---|
| Goose | **28–37K** | 0.2–0.3 |
| OpenHands-SDK | ~841K | — |
| OpenCode | 1.1–1.5M | 2.0–2.16 |

Pass rates differed by only 0–8pp. **The 20–40× spread is context accumulation and idle turns, not intelligence.** If we inherit OpenHands' defaults we inherit a ~840K profile, and row 19 collapses.

This is why row 4 says **replace**, not keep. The repayment plan is specific:

- **Our own context plane**, not the SDK condenser: typed dependency-linked episodes, deterministic priority eviction, a never-evictable invariant set (task contract, standing instructions, safety constraints), and a post-compaction validation pass. This also closes the *governance decay* gap ([research.md](research.md) §4.5) that no shipped harness addresses.
- **Steal Goose's actual trick** where it applies: cheap structured environment priming beats lazy exploratory tool-calling. The budget policy in row 3 chooses eager-prime vs defer per task shape — that is the synthesis the greedy plan couldn't express.
- **No-action-turn detection as a first-class breaker** (LangChain's doom-loop middleware moved Terminal-Bench 52.8 → 66.5 on harness work alone).
- **Gate it.** M4 sets a token ceiling per solved task in CI; a regression fails the build the way a test does. Target: **within 2× of Goose at ≥ Goose's pass rate.**

Naming the debt at selection time, with the number, is the part greedy selection never does.

---

## 5. Proving 4.5/5 instead of claiming it

[Harbor](https://github.com/harbor-framework/harbor) is the official Terminal-Bench 2.0 harness and already hosts **Terminus-2, Claude Code, Codex CLI, Gemini CLI, OpenHands, Antigravity SDK, Grok Build, Mini-SWE-Agent**. Registering a custom agent is `harbor run -d terminal-bench@2.0 --agent path.to.agent:OptimusAgent` — subclass `BaseAgent`.

That single adapter buys three things at once:

1. **Benchmark parity on the field's own instrument** — no self-reported numbers, no bespoke suite nobody trusts.
2. **Achilles's harness tournament, for free and against real opponents.** Its idea was right; building our own arena was the mistake. Harbor *is* the arena.
3. **Evals as RL environments later** — the verifiers taskset/harness/runtime split composes with this at no extra cost.

Publish per run: pass^k, **tokens per solved task**, no-action turns, wall time, unsafe attempts refused, operator interventions. Nobody publishes the middle three. That is the receipt.

---

## 6. Where the OS plane attaches

Unchanged in content, changed in status: [architecture.md](architecture.md) is now the spec for **one plane, mounted at M6**, on a harness that already scores without it. UFO2, Windows-MCP, PowerShell.MCP and Agent Workspace all arrive as MCP servers and library adapters behind the same Gate, the same Ledger, the same reversal journal, the same meter.

Which is the point: because invariants 1–5 hold, the desktop plane is *just another plane*. The Descent (skills falling from vision → UIA → API → script) becomes an instance of row 7's general graduation machinery rather than a bespoke subsystem. **Building the generic harness first makes the OS differentiator cheaper, not later.**

---

## 7. Build order

| # | Milestone | Done when |
|---|---|---|
| **M0** | Fork OpenHands SDK. **Gate** as a security analyzer (trust labels + CEL + handles + instance-bound grants). **Ledger**: sign and chain its event store. | Adversarial suite passes: path traversal, target substitution, forged receipt, law-tightened-after-park, redirect-to-private-space — all refused, all named. |
| **M1** | **Context plane** (typed episodes, invariant set, validation) + **Economy** (per-action metering). Three-mode tool policy over the MCP gateway. | Tokens-per-solved-task measured on a local task set and inside 2× of Goose. |
| **M2** | **Tool planes mounted**: code-mode kernel, open-codebase-index, CDP/Playwright MCP, LSP. Compensation journal + `optimus undo`. | A multi-file refactor + web research task runs end-to-end, fully metered, fully reversible. |
| **M3** | **Harbor adapter.** Terminal-Bench 2.1 + SWE-bench Verified, published with the full metric set. | We appear on the board next to Terminus-2 and Codex CLI, with numbers nobody else prints. |
| **M4** | **Surfaces**: TUI + web over the REST/WS core, priority-queue steering, confidence signalling, pre-flight dry-run, ACP client, AG-UI emitter. | Optimus is drivable from Zed/JetBrains and any AG-UI frontend; interrupt/steer/enqueue works mid-loop. |
| **M5** | **Skills + graduation**: SKILL.md, Ludus batteries, promotion diffs, revert, demotion on drift. Mem0 mounted over Ledger projections. | A repeated task's cost falls measurably across runs, with a reviewable promotion diff. |
| **M6** | **OS/desktop plane** — [architecture.md](architecture.md). Agent Workspace feasibility spike **first**. | A watched Office/browser task runs under the same Gate, Ledger and meter as a code task. |
| **M7** | Multiplayer (read/write sets), durability (Temporal), OS-level policy enforcement. | — |

**M0–M3 is the release.** It is a generic harness at ~4.5 with published receipts, and it exists before any Windows risk is taken.

---

## 8. Your question, answered directly

**Yes — Python core, and build the generic Apex harness first. I'm confident, and the deciding facts are not aesthetic:**

1. **OpenHands SDK is MIT, Python, embeddable-not-framework, and has a *pluggable security analyzer* with pre-execution action validation and an event-sourced audit trail.** That is a purpose-built socket for the exact thing that is ours. Nothing in the TS world offers that — DeepSeek Harness has no authorization model at all, and Prime Agent's continual harness has rollback but no external verifier.
2. **The two arguments for TypeScript both dissolve** (§1.2). We keep code-mode and we keep effect-reversibility without paying for a Node spine.
3. **Python is where the deep integrations live** — UFO2, Windows-MCP, browser-use, Mem0, verifiers, Harbor, and Achilles's already-tested trust plane. Tool servers are language-neutral over MCP; *libraries* are not, and UFO2 is a library we want.
4. **Harbor makes proof cheap.** One adapter class puts us on the same board as Claude Code and Codex CLI.

The one thing I'd hold you to: **§4 is not optional.** Adopting OpenHands means adopting a 20–40× token disadvantage unless the context plane is replaced in M1, before anything else is built on top. If that slips, we ship a well-governed harness that is expensive to run, and row 19 — the row nobody else even competes on — is lost.

---

*Next: M0. Fork the SDK, implement the Gate as a security analyzer with handle issuance, sign the event store, and write the adversarial suite that the Seven Laws never had.*

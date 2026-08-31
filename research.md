# Optimus — Pass 1: Global Research

**Scope:** the state of the art in agent harnesses as of **26 August 2026**, surveyed *deliberately without reading `E:\bellona` or `D:\local-sovereign-ai`*, so nothing here is shaped by what we already built.

**Purpose:** build the target. Pass 2 does the reality check against those two codebases. This document is the yardstick — a per-component map of who is world-best at what, and precisely what is worth taking from each.

---

## 0. How to read this, and how much to trust it

- My training data ends **May 2026**. Everything dated after that is reconstructed from live web search on 26 Aug 2026. Primary sources (arXiv PDFs, GitHub READMEs, official docs, Linux Foundation press) are unmarked; secondary tech blogs are marked `[2nd]` and should be re-verified before we bet architecture on them.
- **Benchmark numbers are the least trustworthy thing in this field right now.** Three sources report three different "best" SWE-bench Pro scores (59.1% / 80.0% / 47.1%) for the same period because they use different scaffolds and splits. Every number below carries its source and whether it is vendor-reported.
- Star counts move weekly in 2026. Treat them as order-of-magnitude signals of ecosystem gravity, not precision.
- Where a claim would change our architecture, I flag it **[verify]**.

---

## 1. The one-paragraph state of the field

The consequential shift of 2026 is that **the harness — not the model — became the primary determinant of agent performance**, and the field now knows it and names it. "Harness engineering" went from slang to a formal discipline with a survey, a taxonomy, benchmarks that report harness–model *pairs* instead of model names, and vendor engineering blogs (OpenAI, Anthropic, LangChain, Microsoft, Red Hat, Martin Fowler) treating it as a first-class practice. Three structural things happened on top of that: (a) **the plugin/kernel wars** — DeepSeek shipped a harness where every component including the agent loop is a hot-swappable, *reversible* plugin, while Pi went the opposite way with four tools and a sub-1000-token prompt; (b) **the harness started writing itself** — Prime Intellect shipped a production harness that CRUD-edits its own prompts, skills, sub-agents and memory from its own trajectory, and a whole research literature (an L0–L5 self-improvement ladder) formed around it; and (c) **the interop layer solidified** — MCP went stateless, ACP became the LSP-of-agents with 50+ registered agents, SKILL.md became a cross-vendor standard under the Linux Foundation, and A2A/AG-UI filled agent↔agent and agent↔frontend. The white space left over is not "another CLI." It is: nobody has durable cross-session *learning* that survives evaluation, nobody has OS-level enforcement that is also ergonomic, and nobody has one agent identity genuinely present across every surface a person uses without it being a security disaster.

---

## 2. Scoreboard — the benchmarks that matter, and who leads

| Benchmark | What it really measures | Leader (Aug 2026) | Notes |
|---|---|---|---|
| **ARC-AGI-2** | Novel abstract reasoning | GPT-5.6 Sol **92.5%**, Claude Opus 5 90.4% (25 Jul) | Human *average* 66%. Saturating. [[BenchLM](https://benchlm.ai/benchmarks/arcAgi2)] `[2nd]` |
| **ARC-AGI-3** | Interactive novel-environment reasoning | **Prime Agent + Opus 5: 95.5% Best@1**, 99.97% Best@3 | Human expert baseline 95.4%. **Vendor-reported, not independently replicated.** Bare frontier models score **<1%** — the entire delta is scaffolding. [[Prime Intellect](https://www.primeintellect.ai/blog/prime-agent)] |
| **Terminal-Bench 2.0 / 2.1** | Real terminal work, harness+model jointly | GPT-5.6 Sol (xhigh) **89.5%** on v2.1 | 89 curated tasks; the board tracks **101 agents across 23 distinct scaffolds**. Reference harness Terminus-2; Terminus-KIRA is a fork targeting its failure modes. [[tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.1)] |
| **SWE-bench Pro** | Real multi-file repo work | Split-dependent: **80.3%** (vendor aggregate) vs **61.5%** (Scale's standardized SEAL harness) vs **59.1%** (Scale public) | The 20–30pt spread *is* the scaffold effect. [[BenchLM](https://benchlm.ai/benchmarks/swe-bench-pro)] `[2nd]` |
| **OSWorld-Verified** | Real desktop GUI control, 369 tasks | Qwen3.8 Max **86.1%** (21 Aug); Claude Mythos 5 / Fable 5 ~85% | Independently-validated variant created after self-report inflation. Best *open* system (Agent S3 w/ bBoN) sits at 63.5% on the tracking board. [[Steel](https://leaderboard.steel.dev/leaderboards/osworld/)] `[2nd]` |
| **GAIA (HAL standardized)** | General assistant: reasoning + web + tools | ~65% ceiling — at **$665/run** for the top config | Cost-per-point is now a first-class axis. [[HAL](https://arxiv.org/pdf/2510.11977)] |
| **BrowseComp-Plus** | Deep research, isolating retriever from agent | Fixed ~100K-doc corpus (ACL 2026) | Built because search-time contamination in live-web deep-research benchmarks is measurable and large. [[repo](https://github.com/texttron/BrowseComp-Plus)] |
| **WebVoyager** | Live-web browser agents | Not comparable across projects — browser-use quotes 89.1%; a cross-field survey puts the best credible result at 64.4%, from a benchmark co-author | Treat all browser-agent marketing numbers as unusable. `[2nd]` |
| **OOLONG / ManyIH** | Long-context agentic recall | Prime Agent 0.700 vs 0.420 baseline; 0.874 vs 0.556 (Pairs) | Vendor-reported. |
| **SkillLearnBench** | Continual skill *learning*, 20 tasks / 15 sub-domains | **No method leads across all tasks, and scaling to stronger LLMs does not reliably help** | The most important negative result of the year. [[arXiv](https://arxiv.org/pdf/2604.20087)] |

### 2.1 The four numbers that should govern our design

1. **Cursor: the same model scored 46% vs 80% depending on harness** — wider than the gap between model tiers. `[2nd]`
2. **The Scaffold Effect (arXiv [2607.22585](https://arxiv.org/html/2607.22585)):** across Goose / OpenCode / OpenHands-SDK on 50 Terminal-Bench Pro tasks, **pass rates varied only 0–8pp but tokens-per-solved-task varied up to 40×** — Goose 28–37K, OpenHands ~841K, OpenCode 1.1–1.5M. Drivers: per-turn context accumulation, and **no-action turns** (Goose 0.2–0.3/task vs OpenCode 2.0–2.16/task).
3. **LangChain moved a coding agent 52.8% → 66.5% on Terminal-Bench 2.0 — Top 30 to Top 5 — changing only the harness**: self-verification prompts, environment-context injection, and middleware hooks that detect doom loops.
4. **Claude Opus 4.5: 52.1% vs 57.8% while consuming 256.9M vs 3.9M input tokens** depending on harness. `[2nd]`

> **Consequence for Optimus:** *tokens-per-solved-task* and *no-action-turns-per-task* must be first-class metrics in our eval loop from day one, on equal footing with pass rate. Almost nobody reports these. Matching the best pass rate at a tenth of the tokens is by itself a defensible world-best claim.

---

## 3. The general-purpose harnesses — the field, tiered

### Tier A — the ones defining the frontier

**Prime Agent** — Prime Intellect, MIT, 6 Aug 2026. *The most architecturally radical release of the year.*
- **Recursive Language Model (RLM):** context is a *variable*; sub-agent delegation is a *function call inside a REPL*. The model's only tool is a **persistent IPython kernel**. No tool schemas at all.
- **Sub-agents as async function calls:** `await rlm("sub-task")` spawns a full `prime-agent` with its own model, kernel, session tree and history; returns a child handle immediately; `agent_message.send(...)` steers it mid-flight. Parallel fan-out is free.
- **Continual Harness:** harness state formalized as **H = (ρ, G, K, M)** — prompts, sub-agents, skills, memory — each exposing identical `create_X() / update_X() / delete_X()` plus retrieval. The agent CRUDs its own harness from its own trajectory.
- **`/refine`:** reads the trajectory and applies minimal CRUD edits, in two phases — background planning (non-blocking) then fast application (briefly blocking at turn boundaries). **The base system prompt is immutable; only the harness layer mutates; full rollback.**
- **Compaction replaced by garbage-collector sub-agents** that asynchronously clean main context while full history stays addressable.
- Built on top of `pi`. [[blog](https://www.primeintellect.ai/blog/prime-agent) · [repo](https://github.com/PrimeIntellect-ai/prime-agent)]
- **Steal:** immutable-core / mutable-harness split with rollback; async GC-agent compaction; sub-agent-as-function-call; the CRUD surface over harness state.

**DeepSeek Harness (`dsh`)** — DeepSeek-AI, MIT, dev preview 13 Aug 2026. *The most radical modularity.*
- **"Everything is a plugin."** Model adapter, tool registry, session log, sandbox, storage, scheduling, UI — **and the agent loop itself**. "No privileged core to patch."
- Built on **Cordis**, a meta-framework of **spatiotemporal composability** (Peking University + DeepSeek-AI, 88-page preprint dated 13 Aug 2026), battle-tested since 2019 in the Koishi chatbot framework:
  - **Temporal composability:** when a plugin is removed, *every* side effect is reverted — listeners, connections, mutated shared state. Reversible mount / unmount / hot-reload.
  - **Spatial composability:** dependencies may appear, disappear or change at runtime, and affected "Fibers" react to the new topology.
  - Mechanically: effect tracking + coeffect resolution + a declarative component loader.
- TypeScript/Node core with Python in the loop; web UI on `127.0.0.1:3080`. ~135K stars in four days. [[repo](https://github.com/deepseek-ai/deepseek-harness) · [Cordis](https://github.com/cordiverse/cordis) · [paper](https://github.com/cordiverse/paper)]
- **Steal:** *reversible* plugin lifecycle with guaranteed effect rollback is the best single idea in harness architecture this year. It is what makes hot-swapping and self-modification safe instead of terrifying.

**Pi** — Mario Zechner / Armin Ronacher, Earendil, MIT. *The anti-framework.*
- **Four tools: Read, Write, Edit, Bash.** System prompt under 1,000 tokens. The only extra injection is your own `AGENTS.md` (global + project), fully visible and editable.
- Extension model: **the agent writes its own tools** rather than installing MCP servers or plugins. "Software building software."
- ~54–58K stars; powers OpenClaw's inner loop. [[essay](https://lucumr.pocoo.org/2026/1/31/pi/)]
- **Steal:** the discipline. Every capability we add must justify its tokens against Pi's baseline. Pi is the control group for the entire field.

**OpenCode** — MIT. *The default open-source pick and the ecosystem gravity well.*
- Strict **client/server split**: business logic entirely on a JS server; launches a JS backend plus a **Go TUI** as two processes. Desktop and web clients are peers of the TUI, not afterthoughts.
- Built-in **LSP** with 20+ auto-downloading server definitions — real code intelligence, zero config.
- 75+ providers, ~170K stars, 900+ contributors. [[docs](https://opencode.ai/docs/server/)]
- **Steal:** headless-core-first. If the core is a server speaking a protocol, TUI / web / voice / mobile / CI are clients for free. **Do not** steal its context accumulation — it is the worst offender in the Scaffold Effect study.

**Claude Code** — Anthropic, proprietary; SDK and some components open. *Reference implementation of the primitive set.*
- The primitive stack everyone copied: `CLAUDE.md` / skills / subagents / slash commands / **hooks** / MCP / permission modes / checkpoints / sandboxing.
- **Dynamic Workflows:** orchestration moves into **JavaScript** instead of asking the model to decide delegation each turn — fan-out across subagents, variables, branching, repeat-until-condition, cross-checking with independent agents, filtering low-confidence results.
- Subagents are isolated instances with their own context window, tool permissions and model; the lead sees only the final summary.
- **MCP Tool Search** from v2.1.7: when MCP tool descriptions exceed **10% of context budget**, the client switches to deferred loading.
- **Steal:** the hook event taxonomy (PreToolUse / PostToolUse / SessionStart / SubagentStop…) is the de facto standard — match its names for ecosystem compatibility. And steal Dynamic Workflows: deterministic orchestration in real code beats model-decided delegation for anything repeatable.

**Codex** — OpenAI, CLI open. *The best-documented harness in existence.*
- OpenAI published the internals: *Unrolling the Codex agent loop* (23 Jan 2026), *Unlocking the Codex harness: how we built the App Server* (Feb 2026), *Harness engineering*, and *Codex as a platform: build on the open agent harness* (19 Aug 2026) — stating explicitly that **the harness, not the model, is the reusable asset**.
- **App Server:** bidirectional **JSON-RPC** API carrying streaming progress, tool use, approvals and diffs — this is how you embed an agent inside someone else's product.
- Lifecycle hooks at SessionStart / PreToolUse / PostToolUse for deterministic guardrails "without relying on prompt-level trust."
- **Windows:** native standalone app (4 Mar 2026), **native sandbox**, Microsoft Store distribution, three install paths with different security profiles, and PowerShell↔WSL2 command switching within a single task.
- **Steal:** the App Server pattern, and the Windows sandbox+WSL duality — directly relevant to us.

### Tier B — strong, opinionated, worth raiding

| System | License | Uniquely best at | The idea to take |
|---|---|---|---|
| **OpenClaw** (Steinberger → OpenClaw Foundation) | MIT | **Presence.** Messaging-native personal agent across Signal / Telegram / WhatsApp / Discord / iMessage. 247K stars, 47.7K forks by Mar 2026 — the largest agent community on earth. TS + Swift. Skills are directories with `SKILL.md`, precedence bundled→global→workspace. | Meet the user where they already are, not where the terminal is. Also **the cautionary tale**: Cisco found a third-party skill doing silent exfiltration + prompt injection; the MoltMatch incident (agent autonomously created a dating profile); Chinese state orgs banned it in Mar 2026 over unauthorized deletion and leaks. |
| **Hermes Agent** (Nous Research) | Open | **Long-running multi-surface persistence.** Gateway across Telegram/Discord/Slack/WhatsApp/Signal/email/CLI with cross-platform conversation continuity. 200+ models. **Seven terminal backends** — local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox — serverless ones ~free when idle. | **Two hook surfaces with different trust models:** in-process plugin lifecycle hooks (can block / rewrite / pass) vs filesystem gateway hooks (user shell or Python scripts on gateway-start, agent-step). Plus "sessions as infrastructure", **separating tool *registration* from tool *exposure***, lineage-based context compression, and layered recall (bounded curated memory → FTS5 session search → hybrid doc search → wiki KB → pluggable external providers). |
| **Goose** (Block → Linux Foundation AAIF) | Apache 2.0 | **Token frugality.** 28–37K tokens per solved task vs OpenCode's 1.1–1.5M, via eager file-tree pre-injection and near-zero no-action turns. Foundation-governed since April. | Eager, cheap, structured environment priming beats lazy exploratory tool-calling by an order of magnitude on cost. |
| **OpenHarness / Ohmo** (HKUDS) | MIT | **Completeness in one readable Python codebase.** 10 subsystems: Engine (streaming tool-call loop, retry, parallel exec), 43+ tools, Skills, Permissions (multi-level modes + path rules + command denial), Coordinator (subagent/team), Memory (`MEMORY.md`), 54 slash commands, plugins compatible with `anthropics/skills` and claude-code. React/Ink TUI. 15.5K stars. | **`--dry-run` pre-flight config verification** returning ready/warning/blocked without touching a model — a genuinely underrated primitive nobody else ships. Also: runs on existing Claude Code / Codex *subscriptions* rather than API keys. |
| **jcode** (Rust, YC 2026) | Open | **Resource efficiency + native multi-agent awareness.** ~27.8MB per session vs Claude Code's ~386.6MB; ~117MB for ten parallel sessions vs ~2.3GB; claimed 245× faster start. When two agents share a repo, **the server notifies agent B that agent A edited a file B had read**; agents DM each other and broadcast. Semantic memory graph. | The **shared-repo mutation notification bus** is the right primitive for parallel agents — better than pure worktree isolation, because it handles the case where isolation is the wrong answer. |
| **LangChain Deep Agents** | MIT | **Middleware as the harness abstraction.** Middleware compresses history, offloads large tool results, isolates context via subagents, applies prompt caching. Filesystem working memory + progressive skill disclosure + layered security. | Middleware chain beats monolithic loop — and their 52.8→66.5 jump is the best-documented proof that pure harness work moves benchmarks. |
| **Microsoft Agent Framework 1.0** (GA 2 Apr 2026) | Open | **Enterprise policy plumbing.** AutoGen + Semantic Kernel converged. **Microsoft Execution Containers (MXC)**: cross-platform policy-driven execution across Windows and WSL, file/network policy declared via Intune and enforced at runtime. CodeAct built in. | For a Windows-first system this is the closest thing to a native OS-level agent sandbox from the platform vendor. **[verify]** whether MXC is usable outside Intune-managed environments. |
| **Google Antigravity 2.0** (I/O, May 2026) | Proprietary | **Best sandbox ergonomics of any shipped harness.** Native OS primitives with *zero startup overhead* — **nsjail on Linux, sandbox-exec on macOS, AppContainer on Windows** — instead of containers or VMs. Subagents run async; "self" subagents clone the caller's instructions and toolset and **automatically inherit** the parent's allowed command prefixes, file scopes and sandbox settings; parent retains access to subagent worktrees. Gemini CLI is being folded into it. | **Permission inheritance for subagents** is the correct default and almost nobody implements it. Native-OS-primitive sandboxing beats Docker for a local-first system. |
| **Buzz** (Block, 21 Jul 2026) | Apache 2.0 | **Cryptographic human-agent parity.** Every human *and* agent gets an independent **Schnorr keypair**; every message, reaction, workflow step, review approval, canvas change and git event is a signed **Nostr** event in one append-only log. Model-agnostic over **ACP** — Claude Code, Codex, Goose all plug in. | Identity and audit as a *cryptographic* property rather than a database table. The only credible answer to "who did what" in a multi-agent org. |
| **Amoeba** (closed) | Closed | **Multiplayer coordination.** Workspaces / sessions / lanes / Mission Control / "shared project brain". Detects overlapping agent work *before tokens burn*; agents join, divide remaining work, or ask when no safe move exists. | **Collision detection as a pre-flight step, not a merge-time step.** |
| **OpenBot** (CopilotKit) | MIT | **Local-first event bus as the OS.** `GET /api/events` (SSE), `POST /api/publish`, `GET /api/state`; everything under `~/.openbot`; Melony runtime; agents defined by `~/.openbot/agents/<id>/AGENT.md`; a deterministic **non-LLM state agent** alongside the LLM agent. Fail-closed gateway; works with any AG-UI harness. | **A deterministic non-LLM agent sharing the same event API as the LLM agent.** That is how automation and intelligence become interchangeable. |
| **AgentSpace** (HKUDS) / **QM** (YC) | Open | Human+agent shared workspace with scheduling, capability sharing and governance (AgentSpace); per-person and per-room scoped memory, files, credentials, permissions, schedules and durable sandboxes via Slack + web (QM). | The multi-tenant unit is the **room**, not the user. Credentials and memory scope to rooms. |
| **Aider · Cline · Roo/Kilo · Continue · Zed · Kimi Code · Qwen Code · Devin/Windsurf · Factory Droid · Cursor · Mastra · DeerFlow · OpenHands · SWE-agent** | mixed | Aider: disciplined git-native editing, tree-sitter repomap, auto-commit per change. Cline: permission-gated propose→approve, 8M devs. Zed: agent native to a Rust core, lowest latency. Kimi: swarm coordination up to ~100 sub-agents. Factory: per-workflow scoped agents for orgs. Mastra: local Studio UI with observability built in. OpenHands/SWE-agent: the research lineage everything descends from. | Aider's auto-commit-per-change is the cheapest possible checkpoint/undo system. Cline's propose→approve is the right default for destructive operations. |

---

## 4. Component-by-component: who is best in the world, and what to take

This is the steal list. The thesis the user set — *80–90% of best-in-class on every axis, plus one thing we are best in the world at* — requires knowing the actual ceiling on each axis.

### 4.1 The formal object we are building

The 2026 survey (Meng et al., arXiv 2605.29682; 110+ papers, 23 systems) formalizes a harness as a six-tuple with labeled-transition-system semantics, distinguishing safety properties (invariants that must always hold) from liveness properties (eventual progress):

> **H = (E, T, C, S, L, V)**
> **E** Execution loop — observe/think/act cycle, termination conditions, error recovery
> **T** Tool registry — typed catalog, routing, monitoring, schema validation
> **C** Context manager — what enters the window, compaction, retrieval
> **S** State store — persistence across turns and sessions, crash recovery
> **L** Lifecycle hooks — auth, logging, policy enforcement, instrumentation
> **V** Evaluation interface — action trajectories, intermediate states, success signals

Prime Intellect's orthogonal formalization of the *mutable* layer is **H = (ρ, G, K, M)** — prompts, sub-agents, skills, memory. The two compose: (E,T,C,S,L,V) is the machine; (ρ,G,K,M) is what the machine is allowed to rewrite about itself. **We should adopt both explicitly and name our modules after them** — it makes the architecture legible to anyone who has read the literature, and it makes gaps obvious.

Sources: [[Awesome-Agent-Harness](https://github.com/Gloriaameng/Awesome-Agent-Harness)] · [[RUCAIBox survey](https://github.com/RUCAIBox/awesome-agent-harness)] · [[awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering)]

### 4.2 Agent loop / kernel — **best: Prime Agent (RLM), runner-up DeepSeek Harness**

The frontier idea is that **the loop should not be a fixed control-flow graph**. Two competing answers:

- **Code-as-loop (Prime Agent, "Code as Agent Harness" arXiv [2605.18747](https://arxiv.org/pdf/2605.18747)):** a persistent interpreter is the only tool; orchestration, tool use and delegation are all just program text the model writes. Eliminates the schema/implementation gap; allows the model to treat *code as data* and rewrite its own logic at runtime. Documented limitations: execution safety, debugging model-generated code, and dependence on strong codegen.
- **Loop-as-plugin (DeepSeek Harness):** the loop is one mountable component among many, so you can ship several loops (ReAct, plan-execute, tree-search, deterministic workflow) and swap per task.

Also relevant: *A Scheduler-Theoretic Framework for LLM Agent Execution*, and "Agents Learn Their Runtime: Interpreter Persistence" — persistent interpreter state is itself a capability, not just an optimization.

> **Take:** both. A plugin-mounted loop registry *whose default loop is code-first*. That combination does not exist in one system today.

### 4.3 Plugin / extensibility architecture — **best: Cordis (DeepSeek), by a wide margin**

Everyone has plugins. Only Cordis has **reversibility as a formal property**. Temporal composability means unmounting a plugin provably undoes its listeners, connections and shared-state mutations; spatial composability means the dependency graph can change shape at runtime and dependents react. This is the difference between "plugins" and "a kernel."

Contrast the alternatives:
- Claude Code / Antigravity: plugins as directories with marker files + MCP definitions + hooks + skills + subagents + rules. Simple, portable, no lifecycle guarantees.
- Hermes: two hook surfaces with explicitly different trust models — this is a *security* insight the others lack.
- OpenBot: plugins over a local event bus with SSE.

> **Take:** Cordis-style effect tracking is the thing to steal outright. Combine with Hermes's trust-tiered hook surfaces so that in-process plugins and user-scripted hooks are never confused with one another.

### 4.4 Tool interface — **best: code mode / MCP Tool Search**

The token economics here are brutal and settled:
- Anthropic reported a Google-Drive→Salesforce workflow going from **150,000 → 2,000 tokens (98.7% reduction)** by having the model write code against tool APIs instead of calling tools directly.
- Cloudflare collapsed **2,500 endpoints ≈ 244,000 tokens → ~1,000 tokens** with Code Mode.
- Claude Code switches to deferred tool loading when MCP descriptions exceed **10% of context budget**.
- Caveat: for 3–4 tools that rarely chain, discovery→inspect→execute adds latency for no gain.

Complementary primitives: **progressive disclosure** (three-tier skill loading), **tool search** (fetch schemas on demand), **TOON** and similar compact encodings, and Hermes's **registration ≠ exposure** split.

> **Take:** a tool layer with three modes — direct schema (few tools), deferred/search (many tools), and code-mode (chained work) — selected automatically by a context-budget policy, not by a config flag. Report the token delta so it is provable.

### 4.5 Context management & compaction — **best: Prime Agent's async GC-agents; strongest research: CWL and CompactionRL**

2026 research verdict: the two heuristics everyone ships — reactive compaction (fires near the token ceiling) and periodic compaction (fires on an interval) — are both **content-agnostic and measurably bad**.

State of the art:
- **Structured eviction (Context Window Lifecycle):** agents annotate trajectories as typed, dependency-linked episodes; a deterministic policy evicts in priority order when over budget. [[arXiv](https://arxiv.org/pdf/2606.11213)]
- **CompactionRL:** RL-trained compaction; best compacted-inference performance on SWE-bench Verified and Terminal-Bench 2.0 under a fixed peak working-context length. [[arXiv](https://arxiv.org/html/2607.05378v1)]
- **Slipstream:** trajectory-grounded *validation* of a compaction — i.e. check the compaction didn't lose something load-bearing.
- **Governance Decay (arXiv [2606.22528](https://arxiv.org/pdf/2606.22528)):** compaction **silently erases safety constraints**, and no shipped method checks whether governance survived the rewrite. Unsafe tool calls follow.
- **Execution instability** under compression is now empirically characterized (arXiv 2608.06503).
- Hermes: **lineage-based** context compression. Anthropic/Claude Code: structured natural-language checkpoints.

> **Take:** typed, dependency-linked episodes with deterministic priority eviction + a validation pass + an explicit **invariant set that is never evictable** (safety constraints, user standing instructions, the task contract). The "governance decay" result is a free, unclaimed differentiator: *no shipped harness verifies that its compaction preserved its constraints.*

### 4.6 Memory & state — **best: no clear winner; the benchmarks disagree with each other**

- **Mem0** — production cross-tool layer, ~51K stars, 100K+ devs. April 2026 algorithm: single-pass hierarchical extraction + multi-signal retrieval; **+29.6 points on temporal queries, +23.1 on multi-hop** over their previous version. Extremely token-frugal: ~1,764 tokens/conversation.
- **Zep / Graphiti** — temporal knowledge graph. Powerful, but Mem0's paper measured **>600,000 tokens per conversation** of footprint, and retrieval immediately post-ingestion often fails until background graph processing completes — disqualifying for real-time.
- **Letta / MemGPT** — stateful agent framework, the academic root (hierarchical context = OS paging analogy).
- **MemPalace** — local-first verbatim storage. **claude-mem** — IDE-plugin scoped.
- **Benchmark politics:** LoCoMo (10 conversations, well-controlled, favored by Mem0) vs **LongMemEval** (up to 1.5M tokens, 500 questions, harder and more realistic, favored by Zep/Cortex/Hindsight). Use LongMemEval.
- Research to mine: A-Mem, MemoryBank, Evo-Memory, ReasoningBank, MemAct (working-memory management as a *learnable policy action*), ByteRover (LLM-curated hierarchical context), Memp (distilling trajectories into maintainable script-like procedures).
- Risk literature is real: **MemSyco-Bench** (sycophancy in agent memory), **MemTrace** (error attribution in memory systems), persona drift detection.

> **Take:** memory must be **local-first, token-budgeted, and attributable** — every retrieved memory carries provenance and a confidence, and every write is traceable to the trajectory that produced it. Nobody ships attribution properly; MemTrace exists because it is missing.

### 4.7 Skills & self-learning — **best: Prime Agent + Hermes in production; ACE + Dynamic Cheatsheet in research**

- **The SKILL.md standard won.** Anthropic opened the spec 18 Dec 2025 — a folder, a Markdown file with YAML frontmatter, three-tier progressive disclosure. Within ~12 weeks OpenAI, Microsoft, JetBrains, Cursor, Gemini CLI, Goose and 25+ others shipped compatible implementations. Governance moved to the **Agentic AI Foundation** under the Linux Foundation (which also stewards MCP). Vercel's skills.sh lists ~89,753 skills. `AGENTS.md` is the project-scoped complement.
- **Hermes** autonomously *creates* SKILL.md files from experience and improves them in use.
- **ACE (Agentic Context Engineering)** — contexts as evolving playbooks via Generator / Reflector / Curator with **delta updates**: +10.6% on agents, +8.6% on finance. [[arXiv 2510.04618](https://arxiv.org/abs/2510.04618)]
- **Dynamic Cheatsheet** — persistent self-curated memory at inference time. **ReasoningBank** — distills strategies from *successes and failures*. **Memp** — trajectories → maintainable procedures. **Voyager / SkillWeaver** — automatic curriculum + self-growing executable skill library.
- **The hard truth — SkillLearnBench:** across 20 verified skill-dependent tasks in 15 sub-domains, evaluated at three levels (skill quality, execution trajectory, task outcome), **no continual-learning method leads across all tasks, and stronger LLMs do not reliably help.** [[arXiv](https://arxiv.org/pdf/2604.20087)]
- **SkillOps** frames skill libraries as self-maintaining software ecosystems; **FederatedSkill** does federated skill evolution across users.
- **Security:** *SkillTester* benchmarks utility *and security* of skills; OpenClaw's exfiltrating third-party skill is the proof this matters.

> **Take:** a skill library that is a *versioned, tested, garbage-collected software artifact* — each skill carries provenance, a regression test, a usage count, and a decay policy. The field has a generate-store-reuse cycle and no maintenance cycle. That gap is enormous and it is exactly where SkillLearnBench says everyone fails.

### 4.8 Self-improving harnesses — the L0–L5 ladder

The cleanest framing in the field, from [[Awesome-Harness-Self-Improvement](https://github.com/leezythu/Awesome-Harness-Self-Improvement)] — *what level of the harness improves itself*:

| Level | What mutates | Representative work |
|---|---|---|
| **L0** | Instruction prompts | APE, OPRO, EvoPrompt, Promptbreeder, ProTeGi, DSPy/MIPROv2, TextGrad, **GEPA** (genetic-Pareto, reads full traces, beats RL on sample efficiency) |
| **L1** | Context & memory | Reflexion, ExpeL, **Dynamic Cheatsheet**, **ACE**, MCE, ReasoningBank, Agent Workflow Memory, Memp, MemAct |
| **L2** | Workflow / graph structure | **ADAS/Meta Agent Search**, AFlow (MCTS over code-graphs), GPTSwarm, AgentSquare, MaAS (agentic supernet), MASS, ScoreFlow, FlowReasoner, EvoAgent, Agent Symbolic Learning, Alita (self-generates and reuses MCP tools) |
| **L3** | Harness / agent code | **STOP**, **Gödel Agent** (monkey-patches its own logic), **Darwin Gödel Machine** (rewrites its codebase over an open-ended archive), SICA, **Self-Harness** (propose→evaluate→accept with regression validation), **AutoHarness** |
| **L4** | Optimizer / meta-harness code | Meta-Harness (proposer searches harness code, returns a **Pareto frontier**), Hyperagents, Ouroboros |
| **L5** | Harness + weights jointly | SIA (per-iteration decision: update harness or weights), **SEAL** (model generates its own finetuning data), Voyager, SkillWeaver |

Two production-relevant results:
- **AutoHarness** (Google DeepMind, arXiv [2603.03329](https://arxiv.org/pdf/2603.03329)): 78% of Gemini-2.5-Flash losses in a chess arena were *illegal moves*. The model synthesized its own code harness via a few rounds of iterative refinement on environment feedback — and **Flash + AutoHarness then beat Gemini-2.5-Pro and GPT-5.2-High** on TextArena. Constraint enforcement in code beats model scale.
- **Observability-Driven Automatic Evolution** (arXiv [2604.25850](https://arxiv.org/pdf/2604.25850)): traces → signal analysis (success rates by category, tool-usage patterns, error frequencies, inefficient paths, confidence) → hypothesis generation → tested refinement. This is the *engineering* shape of self-improvement.

**And the counter-evidence, which matters more than the hype:**
- **Misevolution** (arXiv [2509.26354](https://arxiv.org/pdf/2509.26354), + *Practice Makes Unsafe* arXiv 2608.12851): self-evolution deviates along four pathways — model, memory, tool, workflow. **Safety alignment degrades after memory accumulation; tool creation and reuse silently introduce vulnerabilities.** Affects agents built on top-tier models.
- **Zombie agents:** persistent adversarial control of self-evolving agents.
- **Overfitting:** optimized harnesses **overfit their training distribution** and underperform a generic harness on neighboring tasks — what was learned was task-specific compensation for a model's weak spots, not generalization.
- **Auditability:** auto-optimized harnesses widen train/production skew with **no human-readable audit trail**.
- **LLMs Cannot Self-Correct** (Huang et al.): intrinsic self-correction degrades without an external signal.

> **Take:** self-improvement is only safe with (1) an **immutable core** the loop cannot touch, (2) **regression validation** on every accepted mutation, (3) **full rollback**, (4) a **human-readable diff** of every harness change, and (5) an external verifier — never self-scoring. Prime Agent has 1–3. Nobody ships 4 and 5 properly. **This is one of the strongest candidates for our unique thing.**

### 4.9 Sandbox & execution runtime — **best ergonomics: Antigravity; best isolation: Firecracker; best branching: Morph**

- **Native OS primitives, zero startup cost:** nsjail (Linux) / sandbox-exec (macOS) / **AppContainer (Windows)** — Antigravity's approach and the right one for local-first.
- **microVM:** Firecracker gives each sandbox its own kernel (vs containers sharing the host kernel); **snapshot-restore resumes in 5–30ms**. E2B is the most mature ephemeral provider.
- **Branching:** Morph's differentiator is snapshot **and fork** of agent state — parallel exploration from a common point.
- **Containers:** Daytona had sub-90ms cold starts but **its open-source repo has been unmaintained since June 2026** — do not build on it. Temps is the self-hostable Firecracker option.
- **Windows specifically:** Microsoft Execution Containers (MXC) for Windows+WSL with Intune-declared file/network policy; Codex ships a native Windows sandbox with three security profiles; WSL2 gained a sandboxed AI layer.
- Northflank's 2026 guidance: defense in depth — isolation boundary + resource limits + network controls + permission scoping + monitoring.

> **Take:** for a local-first Windows system, AppContainer/Job Objects + WSL2 + optional Firecracker-in-WSL, with **snapshot-and-fork** as the marquee capability. Fork-the-world for speculative parallel attempts is a capability almost no local harness has.

### 4.10 OS-level policy & enforcement — **best: ActPlane (research), MXC (product). This is the most under-exploited layer in the field.**

**ActPlane** (arXiv [2606.25189](https://arxiv.org/html/2606.25189)) is the most important systems paper for us:
- Compiles a policy **DSL into eBPF programs**, attached via **BPF-LSM** syscall hooks for *preventive* enforcement — so it sees every execution path including subprocesses and shell-outs that tool-call interception misses entirely.
- **Information-flow control:** labels on processes, files and network endpoints propagate monotonically across fork/exec/read/write/connect. Enables policies like *"data from .env must never reach the network."*
- Policy DSL: source, target operation, effect (notify / block / kill), optional temporal gates, and a reason. Example: `kill exec "git" "commit" unless after exec "go" "test" exits 0`.
- **Hierarchical domains:** children inherit parent rules and **cannot weaken them**.
- Results: **75.8% decision compliance, 2.0–3.2× over prompt-filter / tool-regex / FIDES baselines**, with baselines at *near-zero* on indirect execution paths. Overhead **1.9% end-to-end** on trace replay, 6.5–8.4% on kernel builds. Prevents **74% of baseline-unsafe behaviors** on 361 OpenAgentSafety tasks using policies generated from task descriptions alone.
- The empirical finding that reframes everything: **83% of policies in real agent projects require OS-level enforcement, and only 26.4% are self-contained** — 73.6% need project or task context, so *static* rules cannot express them. The agent must *generate* the policy at runtime while the kernel *enforces* it deterministically.

Supporting: eBPF is replacing user-space agents for security observability generally; Falco/Cilium/Tetragon are mature; kernel VFS hooks (`vfs_mkdir`, `vfs_create`) allow in-kernel filtering of filesystem events. Known limit: **syscalls cannot see semantics** — the kernel cannot tell that a prompt injection caused a tool call, so you need an application-context layer above it.

> **Take:** this is a wide-open lane. An agent that **writes its own enforcement policy per task, in a readable DSL, enforced below itself where it cannot lie about compliance** is both a safety story and a capability story — it lets you grant *more* autonomy, not less. On Windows the equivalent primitives are ETW + Windows Filtering Platform + minifilter drivers + AppContainer + Job Objects; there is no eBPF-equivalent parity story published yet. **[verify]** — and if it genuinely doesn't exist, building it is a world-first.

### 4.11 Security & permissions — **best practice exists; nobody has assembled it**

- Prompt injection remains **the leading agentic security failure in production** (OWASP, mid-2026). NIST **IR 8596 (Agentic AI Profile)** now addresses blast radius and scope-limited permissions.
- **Open Agent Passport (OAP)**, March 2026: an open spec that intercepts tool calls **synchronously before execution**, evaluates against declarative policy, and emits a **cryptographically signed audit record**.
- **OPA/Rego** at the tool-calling layer — policy as versioned code, consulted at admission time, so the *policy engine* decides, not the agent.
- Deterministic **lifecycle hooks** (Claude Code, Codex) as runtime enforcement rather than prompt-level trust.
- **MCP tool annotations as a risk vocabulary**; MCP 2026-07-28 hardened authorization.
- Threat literature specific to harnesses: *Trojan's Whisper* (injected bootstrapped guidance), *From Prompt Injection to Persistent Control* (trojan backdoors in harnesses), systematic security evaluations of OpenClaw and variants, **Token-Flow Firewall** (semantic runtime auditing of persistent agents), **AgentHarm**.
- Chilling data point from the harness survey: frontier models achieve **15–35% container escape rates**.

> **Take:** the stack that nobody has assembled end-to-end — OAP-style pre-action authorization + OPA-style declarative policy + ActPlane-style kernel enforcement + Buzz-style signed audit log + Cordis-style reversibility. Every layer exists separately. **Assembling them is a legitimate world-best claim.**

### 4.12 Browser & computer use — **best open: browser-use / Stagehand / Playwright MCP for different jobs**

- **browser-use** — Python, vision + DOM, best for complex multi-step workflows; slowest and priciest because it observes/plans/verifies each step.
- **Stagehand** — TypeScript, three primitives (`act` / `extract` / `observe`); fastest, best for structured extraction.
- **Playwright MCP** / **Chrome DevTools MCP** — browser as a pluggable tool layer for any agent. Right choice if you already have a harness.
- **Skyvern** — the fourth shape.
- OSWorld shows the desktop-GUI ceiling (~86%) is held by closed systems; the best open system is ~22 points behind.
- **The benchmark situation is genuinely broken** — each project quotes a different suite with a different judge against different live sites.

> **Take:** browser access should be a *tool-layer plugin* (Playwright MCP / CDP) with an optional agentic mode, not a bespoke subsystem. And build an internal, reproducible browser eval, because the public ones are unusable.

### 4.13 Codebase intelligence — **best: local-first graphs (CodeGraph, GitNexus), plus LSP**

- The "agentic grep vs semantic index" debate **turned empirical in May 2026 and the index won**: independently measured **97% fewer input tokens** via grepai; **58–70% fewer tool calls** with CodeGraph.
- **Local-first graphs are the winning pattern**: CodeGraph's embedded SQLite graph (47.4K stars in 5 months) and GitNexus's zero-server LadybugDB (~1.2K → 42K stars Apr–Jun) both pre-compute structure **on-device** and serve it over MCP — no cloud, no embeddings API, no code egress.
- `open-codebase-index` (Rust + tree-sitter) combines embeddings + BM25 + branch-aware filtering + symbol lookup + call graph, and explicitly targets OpenCode/Claude/Codex/Pi/jcode/MCP hosts.
- Complementary: **LSP** (OpenCode's built-in 20+ servers), **SCIP** indexes for precise navigation, Aider's tree-sitter repomap, CocoIndex for incremental re-indexing.
- Research: *Code Isn't Memory: A Structural Codebase Index Inside a Coding Agent* (arXiv 2606.22417); OrcaLoca for issue localization.

> **Take:** local-first graph + LSP + BM25 + symbol search, incrementally maintained, exposed as *one* query tool. Zero code egress is both a privacy property and a performance property. This is a solved problem we should simply solve correctly rather than invent.

### 4.14 Protocols & interop — the settled stack

| Protocol | Layer | Status Aug 2026 |
|---|---|---|
| **MCP** | agent ↔ tools | **2026-07-28 spec is final** and the biggest break since authorization: **stateless protocol core**, multi-round-trip requests, header-based routing, cacheable list results, hardened auth, a formal **extensions framework**. Tasks moved out of experimental core into `io.modelcontextprotocol/tasks` (poll-based `tasks/get`, new `tasks/update`). **MCP Apps (SEP-1865)**: servers ship interactive HTML rendered in a sandboxed iframe, with UI templates declared ahead of time so hosts can prefetch, cache and security-review. Many changes are **not backward compatible**. Governed by the Linux Foundation. |
| **ACP** (Agent Client Protocol) | agent ↔ editor/client | JSON-RPC 2.0 over stdin/stdout. Adopted by **JetBrains, Google, GitHub, 25+ agents**; public registry with JetBrains from 28 Jan 2026, **50+ registered agents by late June**; headline feature of Zed 1.0 (29 Apr 2026); Neovim support. VS Code only via a community extension as of June. **This is LSP-for-agents and it won.** |
| **A2A** | agent ↔ agent | Google → Linux Foundation. **150+ organizations**, deep Google/Microsoft/AWS integration, production deployments in supply chain, financial services, insurance, IT ops. Agent Cards (JSON-LD) for discovery; auth schemes declared per agent (OAuth2/OIDC/API key/mTLS). Latency class 50–200ms vs MCP's 2–15ms. |
| **AG-UI** | agent ↔ frontend | Event-driven bidirectional streaming, declarative generative UI, state sync, human-in-the-loop. Supported by LangGraph, CrewAI, Microsoft Agent Framework, Google ADK, AWS Strands, Pydantic AI, LlamaIndex; SDKs in Python, TS, Kotlin, Go, Rust, Java, Dart. |
| **Agent Skills (SKILL.md)** | portable capability | Open standard under the Agentic AI Foundation; ~30+ compatible products; ~89.7K skills on skills.sh. |
| **AP2 / x402** | agent payments | AP2 has 60+ orgs across payments/financial services. x402: HTTP 402 challenge → EIP-3009 signed authorization in an `X-PAYMENT` header → facilitator verification. |
| **OAP** | pre-action authorization | Synchronous interception + declarative policy + signed audit record. |

**Known gap:** *Governance Gaps in Agent Interoperability Protocols* (arXiv 2606.31498) — MCP, A2A and ACP **cannot express** delegation limits, accountability chains or revocation. That is unclaimed ground.

> **Take:** speak MCP (client *and* server), ACP (so we appear in Zed/JetBrains/Neovim for free), AG-UI (so any frontend works), and SKILL.md (so the 89K-skill ecosystem is ours on day one). Interop is cheap leverage — four protocols buy us an ecosystem we do not have to build.

### 4.15 UI/UX and surface presence — **best: OpenCode (terminal), Codex App Server (embedding), OpenClaw/Hermes (presence)**

Patterns worth naming:
- **Interface-agnostic state machine.** The same core loop runs behind a rich TUI locally or fanned out to cloud infra. Events stream to the client via an async generator. This is OpenCode's client/server split and Codex's App Server, and it is the correct base decision.
- **Steering with priority queues.** Decouple input handling so the user can **steer, interrupt, or enqueue mid-loop**. Steering messages land after the current tool and can cancel remaining tools; follow-ups queue until the agent finishes. Very few harnesses get this right and it is the single biggest felt-quality difference in daily use.
- **Confidence signaling.** Every output carries a visible indicator; high-confidence proceeds uninterrupted, low-confidence renders differently and pauses for verification. Emerging 2026 pattern, thinly implemented.
- **Pre-flight verification** (`--dry-run`, OpenHarness): validate config, tools, permissions and readiness *before* burning a token.
- **Real-time collision visibility** (Amoeba, jcode): show what is running, who owns it, what is changing now.
- **MCP Apps**: servers can now ship real interactive UI into the host — a genuinely new UI surface as of the July spec.
- TUI tech: Go (OpenCode), React/Ink (OpenHarness, Claude Code), Rust (Zed, jcode).

> **Take:** priority-queue steering + confidence signaling + pre-flight dry-run + a protocol-first core. Ease of use is not a coat of paint here; it is a consequence of the core being a well-specified server.

### 4.16 Multiplayer / human-agent collaboration — **best concepts: Amoeba (coordination), Buzz (identity), jcode (mechanism)**

Three different answers to the same problem:
- **Amoeba** — pre-flight collision detection; agents divide remaining work or ask when no safe move exists; lanes and Mission Control; shared project brain.
- **Buzz** — cryptographic parity: every human and agent has a Schnorr keypair, every action is a signed Nostr event in one append-only log. Self-hostable, Apache 2.0, model-agnostic over ACP.
- **jcode** — the concrete mechanism: a server that notifies agent B when agent A mutates a file B has read, plus agent-to-agent DM and broadcast.
- **AgentSpace / QM** — governance, scheduling, per-room scoped credentials/memory/sandboxes.
- Research: *Collaborative Document Editing with Multiple Users and AI Agents*; *Envisioning Sensemaking in Multi-Human, Multi-Agent Collaborative Knowledge Work*.
- Survey verdict: **Byzantine fault tolerance and reliable multi-agent orchestration remain unsolved.**

> **Take:** the primitives are read-set/write-set tracking per agent, a mutation notification bus, and signed provenance. Combined, they give collision detection, audit and blame — three features, one mechanism.

### 4.17 Orchestration & durability — **best: Temporal-class durable execution; best agent-native: LangGraph**

- The 2026 framing: *"the RFP question is no longer how your agent handles a 90-second LLM timeout; it is which durable runtime, and what is the replay story."*
- **Checkpointers are not durable execution.** A checkpointer saves state at marked points and hands it back; you still own retry, resume and side-effect deduplication. Temporal's LangGraph plugin runs each node as an Activity and checkpoints at every node, so **the run itself survives**, not just the data.
- Field: Temporal, Inngest, DBOS, Restate, Dagster; LangGraph for agent-native memory/streaming/HITL. Many teams run both.
- Orchestration patterns: Claude Code Dynamic Workflows (JS-defined fan-out/branch/repeat/cross-check), Antigravity async subagents with inherited permissions, Kimi swarm (~100 sub-agents), parallel git worktrees (Conductor, amux, Vibe Kanban — note Bloop shut down 10 Apr 2026 but Vibe Kanban continues community-maintained and fully local).
- Research: AdaptOrch (task-adaptive orchestration), TDP (task-decoupled planning), *Multi-Agent Workflows Often Fail*, LATS, Plan-and-Act.

> **Take:** durable-by-construction from the start. Every agent run is a resumable, replayable, side-effect-deduplicated workflow. Retrofitting this is brutal; building on it is nearly free.

### 4.18 Model layer — routing and local inference

- **Gateways:** Bifrost (Go, ~11µs overhead at 5,000 RPS, 23+ providers, weighted+failover routing, semantic caching, governance, MCP support, Apache 2.0 self-host) — the strongest technical option; LiteLLM (Python, 100+ providers, broadest coverage); OpenRouter (hosted marketplace). **Multi-provider redundancy is now baseline reliability, not premature optimization.**
- **Open weights for agentic work, Aug 2026:** GLM-5.2 tops open-weight coding benchmarks (SWE-bench Pro, Terminal-Bench 2.1); GLM-5.1 is **MIT-licensed** — a real differentiator for commercial use. Kimi K2.6 is built for **sub-agent parallelism**. Qwen 3.6 Plus is the top open-weight pick for demanding agentic coding with a 1M-token context. DeepSeek V4-Flash and Mistral Small 4 bring near-frontier quality to **2-GPU** setups. Muse Glimmer is a 30B dense multimodal model distilled for local agentic work.
- Universal finding: **every open model performs dramatically better inside a structured harness than in raw chat mode.** The harness is worth more to a local model than to a frontier one — which means a great harness is precisely what makes local models viable.
- **Training your own harness is now a thing:** Prime Intellect's **Environments Hub** has 2,500+ open RL environments and 400+ contributors (GA 7 May 2026). **verifiers v1** decomposes an environment into **taskset + harness + runtime** — and notes that *RL environments and agent evals are the same object* (dataset + harness + scoring). prime-rl trains against them directly. *Polar: Agentic RL on Any Harness at Scale* and *Endless Terminals* extend this.

> **Take:** a Bifrost-class routing layer with local models as first-class citizens, and — the sharper idea — **adopt the verifiers taskset/harness/runtime decomposition for our own eval suite**, so that our evals are RL environments for free. If we ever want to fine-tune a small local model to our harness, the infrastructure already exists and costs us nothing extra now.

### 4.19 Observability & evaluation — **best: OTel GenAI + Langfuse; but the conventions have holes**

- **OpenTelemetry GenAI semantic conventions** (CNCF SIG) as of v1.41 define agent / workflow / tool / model spans plus latency and token-usage metrics, and MCP tool recording. **Caveats: nearly all `gen_ai.*` attributes carry Development stability badges (names can change without a major bump), and the conventions do not cover output evaluation, safety scoring or content quality.**
- **Langfuse** — MIT, self-hostable, native OTLP ingestion, trace-based evaluation, prompt management, session replay. Arize, Bedrock AgentCore and Datadog consume the same OTLP endpoint, so instrumentation is portable.
- **HAL (Holistic Agent Leaderboard)** — standardized third-party evaluation across 9 benchmarks, reporting **cost alongside accuracy**.
- Evaluation reality checks worth internalizing: *AI Agents That Matter* (benchmarks over-index on accuracy, weak validity); **METR: many SWE-bench-passing PRs would not merge** — benchmark-passing PRs show a **24.2pp lower human merge rate**; search-time contamination in deep-research benchmarks.

> **Take:** emit OTel GenAI spans natively so any backend works; add the two metrics the conventions omit and the field ignores — **tokens-per-solved-task** and **no-action-turns** — plus a governance-survival check on compaction.

### 4.20 Compute economics — the axis everyone under-reports

The harness survey's ninth open challenge is **compute economics**: ~**1M tokens per task** average, with **13T tokens/week growth doubling every four weeks**. Combined with the 40× spread in tokens-per-solved-task between harnesses, cost is not a footnote — it is the second axis of the frontier. A harness that is 10× cheaper at equal quality is *strictly more capable*, because it can afford 10× more attempts, more verification, more parallel exploration.

---

## 5. The nine open problems — the field's own list of what is unsolved

From the Meng et al. survey, with the status the field itself assigns:

1. **Security & sandboxing** — frontier models achieve **15–35% container escape rates**. No production-grade general solution.
2. **Evaluation & benchmarking** — no unified evaluation across heterogeneous systems; benchmark-passing PRs merge 24.2pp less often.
3. **Protocol standardization** — MCP (2–15ms, tool↔harness) and A2A (50–200ms, agent↔agent) need bridging; none of MCP/A2A/ACP can express delegation limits, accountability chains or revocation.
4. **Runtime context management** — long-context efficiency; curated skill injection alone is worth **+16.2pp**.
5. **Tool use & registry** — schema contracts fix interface errors but not semantic misuse.
6. **Memory architecture** — no standard across flat / episodic / graph stores; no attribution standard.
7. **Planning & reasoning** — adaptive self-correction; **interface design outweighs model capability** as the performance determinant.
8. **Multi-agent coordination** — Byzantine fault tolerance unsolved.
9. **Compute economics** — 1M tokens/task, doubling every 4 weeks.

Plus three the literature adds that the survey underweights:
10. **Governance decay through compaction** — safety constraints silently vanish and nobody checks.
11. **Misevolution** — self-improvement degrades safety alignment via memory, and introduces vulnerabilities via tool creation.
12. **Harness overfitting and auditability** — optimized harnesses overfit their task distribution and leave no human-readable audit trail.

---

## 6. Where the white space actually is

Cross-referencing what everyone ships against what nobody ships, five gaps are real, large, and buildable:

**A. Verified self-improvement.** Prime Agent proved a harness can rewrite itself. Nobody has shown it rewrites itself *correctly* — no regression gate, no human-readable diff of harness changes, no external verifier, no defense against misevolution, and SkillLearnBench says every continual-learning method fails somewhere. A harness where **every self-modification is a reviewable, tested, revertible commit against a frozen eval suite** would be the first trustworthy one. This composes perfectly with Cordis-style reversibility.

**B. Kernel-enforced, agent-authored policy.** ActPlane showed 83% of real agent policies need OS-level enforcement and 73.6% need runtime context — so the agent must author the policy and the kernel must enforce it. On Linux that is eBPF+BPF-LSM. **On Windows nobody has published an equivalent.** Building the Windows story (ETW + WFP + minifilter + AppContainer + Job Objects, driven by the same DSL) is plausibly a world-first, and it is the layer that lets us grant *more* autonomy safely rather than less.

**C. Compaction that provably preserves invariants.** Governance decay is documented, measured, and unaddressed by every shipped harness. Typed episodes + never-evictable invariant set + a post-compaction validation pass is a small amount of engineering for a claim nobody else can make.

**D. Skill libraries with a maintenance cycle.** The field has generate→store→reuse. It has no garbage collection, no regression tests per skill, no provenance, no decay, no conflict detection between skills. SkillOps names the problem; nothing ships the solution. With 89K skills on one marketplace and a documented exfiltration incident, this is urgent, not academic.

**E. Cost as a first-class capability.** 40× token spread between harnesses at equal pass rate, and nobody optimizes for it deliberately. Tokens-per-solved-task as a *targeted, reported, regression-tested* metric is both a differentiator and the thing that makes local models genuinely usable.

---

## 7. Sparks — ideas that do not exist yet

Offered as raw material for pass 2, roughly ordered by how buildable-to-impactful they are.

1. **The Ledger.** One append-only, signed log of everything: every tool call, every context eviction, every skill mutation, every policy decision, every kernel-enforced denial. Buzz proved signing works for humans+agents; ActPlane proved kernel decisions are loggable; nobody has unified them. If the ledger is the *only* source of truth, then compaction, memory, audit, replay, undo, multiplayer coordination and self-improvement review all become **views over one structure** instead of six subsystems. This is the simplicity-from-one-spark idea: *the harness is a log, and everything else is a projection of it.*

2. **Fork-the-world speculative execution.** Morph has snapshot+fork; nobody uses it as an *agent loop primitive*. When the agent hits a genuine fork in the road, snapshot the whole world (FS + process + context), run N branches in parallel, score them against the task contract, keep one, discard the rest atomically. Cordis's reversibility gives us the in-process half; microVM snapshots give us the out-of-process half. This converts "the agent guessed wrong and wasted 200K tokens" into "the agent explored three options and kept the best."

3. **The immune system.** Misevolution says self-improvement degrades safety silently. Run a permanent adversarial sub-agent whose only job is to attack the harness's own recent mutations — replay the frozen eval suite against every accepted skill/prompt/policy change, and auto-revert regressions. Cheap, continuous, and it directly addresses the field's loudest unsolved risk.

4. **Policy the agent writes about itself, enforced beneath itself.** Per-task, the agent emits a readable contract (`no network after reading .env`; `no commit until tests pass`) into a DSL enforced below the agent where it cannot lie about compliance. Its value is not just safety: a *provably* constrained agent can be given far more autonomy, which is the actual liberation move.

5. **Read-set / write-set as the universal coordination primitive.** Track what each agent (and human) has read and written. That single mechanism yields Amoeba's collision detection, jcode's mutation notifications, cache invalidation for context, blame/provenance, and correct parallelism — four features from one data structure.

6. **The deterministic twin.** OpenBot's non-LLM state agent sharing the LLM agent's event API, taken seriously: every capability exists in both a deterministic and a model-driven form behind the same interface, so the harness escalates to the model only when determinism fails — and every model success becomes a candidate deterministic rule. This is self-improvement that makes the system *cheaper and more reliable* over time rather than more powerful and more entropic.

7. **Evals that are RL environments.** Adopt verifiers' taskset/harness/runtime split so our regression suite doubles as a training environment. Costs nothing now; makes fine-tuning a local model to our harness a configuration change later.

8. **Harness-level prompt caching as an architecture, not an optimization.** Given 1M tokens/task and 40× spreads, design the context layout so the immutable prefix is genuinely immutable across a session — the cost model then rewards architectural discipline instead of punishing it.

---

## 8. What "best in the world" would have to mean

Assembling the above, the target for Optimus is a system that:

- runs **local-first** with a protocol-first headless core (MCP + ACP + AG-UI + SKILL.md), so every UI and every ecosystem is a client;
- has a **reversible plugin kernel** (Cordis-class) with a **code-first default loop** (RLM-class);
- is **token-frugal by design** (Goose-class efficiency at frontier-class pass rates), measured and regression-tested;
- has **typed, invariant-preserving compaction** and **attributable memory**;
- has a **maintained, tested, garbage-collected skill library** that grows from experience;
- **improves itself under a frozen external verifier**, with reviewable diffs and one-click rollback;
- enforces **agent-authored policy at the OS level** — on Windows first, where nobody has;
- is **durable** (resumable, replayable, deduplicated) and **multiplayer-ready** via read/write-set tracking and a signed ledger;
- and treats **cost, safety-survival and no-action turns** as headline metrics, because nobody else does.

Nothing in that list is speculative except the Windows kernel-policy layer and the verified-self-improvement loop — and those two are exactly the places where "best in the world" is still unclaimed.

---

## Appendix — primary sources

**Harnesses:** [Prime Agent](https://www.primeintellect.ai/blog/prime-agent) · [prime-agent repo](https://github.com/PrimeIntellect-ai/prime-agent) · [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) · [Cordis](https://github.com/cordiverse/cordis) · [Cordis paper](https://github.com/cordiverse/paper) · [Pi](https://lucumr.pocoo.org/2026/1/31/pi/) · [OpenCode server docs](https://opencode.ai/docs/server/) · [OpenHarness](https://github.com/HKUDS/OpenHarness) · [OpenBot](https://github.com/meetopenbot/openbot) · [OpenClaw (Wikipedia)](https://en.wikipedia.org/wiki/OpenClaw) · [Amoeba](https://useamoeba.com/) · [AgentSpace](https://github.com/HKUDS/AgentSpace) · [deepagents](https://github.com/langchain-ai/deepagents) · [Antigravity sandbox](https://antigravity.google/docs/cli/sandbox/) · [Antigravity subagents](https://antigravity.google/docs/subagents/)

**Surveys & indices:** [Awesome-Agent-Harness (Meng et al.)](https://github.com/Gloriaameng/Awesome-Agent-Harness) · [RUCAIBox awesome-agent-harness](https://github.com/RUCAIBox/awesome-agent-harness) · [awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) · [Awesome-Harness-Self-Improvement](https://github.com/leezythu/Awesome-Harness-Self-Improvement)

**Key papers:** [The Scaffold Effect](https://arxiv.org/html/2607.22585) · [ActPlane (OS-level policy)](https://arxiv.org/html/2606.25189) · [Inside the Scaffold: taxonomy](https://arxiv.org/pdf/2604.03515) · [Code as Agent Harness](https://arxiv.org/pdf/2605.18747) · [Observability-Driven Harness Evolution](https://arxiv.org/pdf/2604.25850) · [AutoHarness](https://arxiv.org/pdf/2603.03329) · [ACE](https://arxiv.org/abs/2510.04618) · [SkillLearnBench](https://arxiv.org/pdf/2604.20087) · [SkillOps](https://arxiv.org/pdf/2605.13716) · [Structured Context Eviction (CWL)](https://arxiv.org/pdf/2606.11213) · [CompactionRL](https://arxiv.org/html/2607.05378v1) · [Governance Decay](https://arxiv.org/pdf/2606.22528) · [Misevolution](https://arxiv.org/pdf/2509.26354) · [Governance Gaps in Interop Protocols](https://arxiv.org/pdf/2606.31498) · [HAL leaderboard](https://arxiv.org/pdf/2510.11977) · [SemaClaw](https://arxiv.org/pdf/2604.11548)

**Protocols & standards:** [MCP 2026-07-28 spec](https://modelcontextprotocol.io/specification/2026-07-28) · [MCP 2026 roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/) · [Zed ACP](https://zed.dev/acp) · [ACP progress report](https://zed.dev/blog/acp-progress-report) · [A2A at Linux Foundation](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year) · [AG-UI](https://docs.ag-ui.com/introduction) · [Agent Skills](https://agentskills.io/home)

**Infrastructure:** [prime-rl](https://github.com/PrimeIntellect-ai/prime-rl) · [verifiers v1](https://www.primeintellect.ai/blog/verifiers-v1) · [Environments Hub](https://docs.primeintellect.ai/tutorials-environments/environments) · [open-codebase-index](https://github.com/Helweg/open-codebase-index) · [BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus) · [Terminal-Bench leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.1)

---

*Pass 1 complete. Pass 2: reality check — measure `E:\bellona` and `D:\local-sovereign-ai` against every row of §4, identify what is already 80–90%, what is mediocre, what is missing, and which of §6/§7 we actually claim.*

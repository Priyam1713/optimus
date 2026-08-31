> **Superseded as the top-level design by [apex.md](apex.md).** This document is now the
> specification for the **OS/desktop plane** only — one plane among many, attached late.
> Read `apex.md` first for the whole-system architecture and build order.

# Optimus — Pass 3: Architecture

Companion to [research.md](research.md) (the field) and [audit.md](audit.md) (the two prior systems). This is the design.

---

## 0. Verdict on the Windows idea

**Feasible, and it is the right bet — but not for the reason you gave.**

You proposed building OS/app operation *on top of* existing infrastructure. The research says something stronger: **the Windows agent substrate already exists, is MIT-licensed, and is missing exactly the layer Bellona and Achilles were each half-building.**

- **Microsoft Research shipped the desktop agent.** [UFO2 / UFO³](https://github.com/microsoft/UFO) — MIT, TMLR May 2026, LTS. HostAgent + per-app AppAgents, a **unified GUI–API action layer**, hybrid UIA+vision control detection, **speculative multi-action batching that cuts LLM calls ~51%**, RAG over docs/demos/traces, and a **Picture-in-Picture isolated virtual desktop so the agent and the user work at the same time without fighting over the mouse.** Its stated gap: *no permission model, no sandboxing, no audit.*
- **Microsoft shipped the OS primitives.** Build 2026 turned Windows into an agent platform: **Agent Workspace** (a separate contained desktop, the agent running under its own randomly-named local account via an `IsolationSession` service, scoped authorization, Entra-backed agent identity, full attribution), plus **MXC** (policy-driven execution containers spanning Windows and WSL).
- **The community shipped the tool surface.** [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) and [mcp-windows](https://github.com/sbroenne/mcp-windows) expose UIA-based control as MCP — by element name, not coordinates, DPI/theme/resolution independent. [PoshMCP](https://mcpservers.org/servers/cezarypiatek/poshmcp) and [PowerShell.MCP](https://github.com/yotsuda/PowerShell.MCP) auto-generate tool schemas from `Get-Command`/`Get-Help` reflection — **10,000+ PowerShell modules become tools with zero hand-written integrations.**
- **The composition layer shipped.** MetaMCP / local-mcp-gateway: aggregate N MCP servers behind one namespaced endpoint with filtering and RBAC.
- **The learning idea is live research.** GOAL (a GUI agent replays a workflow while a sniffer captures the underlying API calls, producing a parameterized deterministic skill), Skill-DisCo (compile traces into verifiable executable code), Workflow-to-Skill, SKILL.md trajectory mining.

So the honest framing:

> **We are not building a Windows agent. We are building the governance, learning and economy layer over an OS that just became agent-capable — and mounting everyone else's hands and eyes underneath it.**

That is a much smaller build, a much bigger differentiator, and it is exactly the "steal from Microsoft" strategy you opened with. It also means the 4.5/5-on-every-row plan and the Windows dream are **the same project**, not alternatives: the OS work is what makes the generic harness rows score, because a harness that can drive Office, the shell and the browser through one governed path is a better *generic* harness too.

**Where I disagree with the dream, stated plainly:** the graduation loop below will work beautifully where an API exists under the pixels (Office/COM, browser DOM+XHR, anything PowerShell-backed, most settings) and will not work for genuinely GUI-only stateful apps. Expect it to cover perhaps half of real tasks. Half is still transformative, and the other half degrades to "a competent watched GUI agent," which is the current state of the art anyway.

---

## 1. The thesis

Three claims, each measurable, none currently held by any shipping harness:

1. **One authorization path for everything a computer can do** — a file write, a PowerShell line, a UIA click, an MCP call, a mouse move — with trust-labelled provenance and structural deny-before-allow.
2. **The system gets cheaper the longer you use it.** Tasks descend the control ladder from vision → UIA → API → script, each descent verified by a battery before it is trusted. Tokens-per-solved-task is a published, regression-gated number that goes **down**.
3. **Nothing irreversible happens without a human seeing the actual payload**, and everything reversible is reversible in one command.

UFO2 has none of these. Windows-MCP has none of these. Claude Code and Codex have (1) partially and neither of the others.

---

## 2. What we mount vs. what we build

The single most important table in this document. **Mounted = we do not write it.**

| Layer | Component | Source | License | Ours? |
|---|---|---|---|---|
| Desktop agent (GUI+API, PiP, speculative batching) | **UFO2/UFO³** | microsoft/UFO | MIT | mount |
| UIA control surface as tools | **Windows-MCP** / **mcp-windows** | community | MIT | mount |
| Shell/system surface (10k+ modules) | **PoshMCP** / **PowerShell.MCP** | community | MIT | mount |
| Office/app object models | **COM automation** via pywin32 | Microsoft | — | thin adapter |
| Browser | **Chrome DevTools MCP** / Playwright MCP | Google/Microsoft | MIT/Apache | mount |
| Isolated desktop + agent identity | **Windows Agent Workspace / MXC** | Windows 2026 | OS | mount |
| Container/VM execution | **WSL2 + Docker**, Windows Sandbox | OS | — | mount (Achilles already has the bridge) |
| Tool aggregation & namespacing | **MetaMCP**-class gateway | community | MIT | mount |
| Model routing / local inference | **llama.cpp**, Bifrost-class gateway | community | MIT/Apache | mount (Achilles's router is already good) |
| Editor/client presence | **ACP** | Zed/JetBrains | open | implement client |
| Frontend streaming | **AG-UI** | CopilotKit | open | implement emitter |
| Skill format | **SKILL.md** | Agentic AI Foundation | open | conform |
| Observability | **OTel GenAI semconv** → Langfuse | CNCF | MIT | emit |
| **Gate** (one authorization path) | — | — | — | **build** |
| **Ledger** (signed, chained, projected) | — | — | — | **build** |
| **Reversal journal** (compensation) | — | — | — | **build** |
| **The Descent** (skill graduation + demotion) | — | — | — | **build** |
| **Verifier / battery / eval gates** | — | — | — | **build** (harvest both repos) |
| **Watch surface** (narration, yield, undo) | — | — | — | **build** |
| **Economy** (tokens-per-solved-task accounting) | — | — | — | **build** |

Seven things to build. Everything else is configuration and adapters. That is the whole point.

---

## 3. Runtime decision

**Python core. Not Rust.**

This is a reversal of Bellona's bet and I want to be explicit about why, because it is the highest-consequence call here:

- Every component in the mount table that touches Windows is Python or .NET — UFO, Windows-MCP, pywinauto/uiautomation, pywin32, the PowerShell SDK. A Rust core means reimplementing bindings for all of it and **losing UFO entirely**, which is the single largest thing we get for free.
- MCP makes the language boundary free anyway. Any component can be any language behind stdio/HTTP.
- Achilles already *is* this core, tested, with the WSL bridge, the router and the trust plane working.
- Rust remains correct for a hot path later — the ledger writer, the UIA tree differ, the input arbiter — as a native module behind a stable interface. Not the kernel.

Bellona's Rust is not wasted; its *designs* are the specification for the gate, the ledger and the eval gates. Its code is not the starting point (see [audit.md](audit.md) §2).

---

## 4. The architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  SURFACES   TUI · web · ACP (Zed/JetBrains) · AG-UI · voice ·   │
│             WATCH PANE (narration, yield, undo, PiP stream)      │
└───────────────────────────┬─────────────────────────────────────┘
                            │  events (projection of the Ledger)
┌───────────────────────────┴─────────────────────────────────────┐
│  KERNEL — small, boring, replaceable-around                      │
│                                                                  │
│   Loop (code-mode default) ── Skills/Descent ── Memory           │
│              │                                                   │
│              ▼                                                   │
│   ╔══════════════════════════════════════════════════════════╗  │
│   ║  THE GATE — the ONLY path from intent to effect          ║  │
│   ║  resolve → trust → policy → reversibility → audit →      ║  │
│   ║  ISSUE HANDLE → execute → settle → compensate?           ║  │
│   ╚══════════════════════════════════════════════════════════╝  │
│              │ capability handle (not a boolean)                 │
│   ┌──────────┴───────────┐                                       │
│   │  LEDGER  signed · hash-chained · trust-labelled · append-only │
│   │  every other store is a PROJECTION of this                    │
│   └──────────────────────────────────────────────────────────────┘
└───────────────────────────┬─────────────────────────────────────┘
                            │  handles only
┌───────────────────────────┴─────────────────────────────────────┐
│  CONTROL LADDER  (tier is chosen by the Skill plane, not tried) │
│   1 native API   2 CLI/PowerShell   3 COM/app plugin             │
│   4 browser DOM  5 UIA accessibility  6 vision GUI  ← last resort│
├──────────────────────────────────────────────────────────────────┤
│  MOUNTED  UFO2 · Windows-MCP · PoshMCP · CDP-MCP · MCP gateway   │
│  VENUES   user desktop (watch) │ Agent Workspace │ WSL2 │ VM     │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 The Gate returns a handle, not a verdict

The defect that produced Bellona's worst bug ([audit.md](audit.md) §2.4) was that the gate authorized a *string* and the executor received the model's *raw arguments*. The fix is structural, not defensive:

```python
handle = gate.request(CapabilityRequest(
    actor, verb, target, tier, trust, reversibility, intent))
# handle is an opaque, single-use, non-forgeable object carrying:
#   - a RESOLVED target (an open fd, a pinned UIA element, a COM moniker,
#     a validated argv) — never a path string the executor re-parses
#   - the compensation record required to undo it
#   - an expiry and the ledger seq that authorized it
result = executor.perform(handle)     # takes ONLY a handle
```

An executor that cannot accept anything but a handle cannot act on unauthorized input. Path traversal, target substitution and TOCTOU stop being classes of bug. This is object-capability discipline, and it is cheap when you do it on day one.

Policy composition — take the best half of each prior system:

- **Trust labels from Achilles**, enforced at every call site: untrusted-origin input (model output, web, documents, MCP results, collaboration) can never authorize mutation or credential use. This is the CaMeL boundary and it is non-negotiable.
- **CEL rules from Bellona**, grouped structurally into deny → approval → allow so ordering is a property of the data structure. Broken rule denies. No match denies. Refusals name their rule id.
- **Grants bound to the action instance, not the class** — fixing [audit.md](audit.md) §3.2. A grant names an argv digest or a resolved target, not `execute:workspace`.

### 4.2 Reversibility is a type, and it decides the gate's behaviour

Every action declares which class it belongs to. Policy keys on the class.

| Class | Mechanism | Gate behaviour |
|---|---|---|
| **Overlay-reversible** | file writes staged in a DiffSandbox | free; the agent works unattended |
| **Compensation-reversible** | registry (prior value saved), created files, started processes, app edits with a recorded inverse | allowed; inverse written to the Ledger *before* the act |
| **Snapshot-reversible** | whole venue (Agent Workspace / VM checkpoint) | allowed inside an isolated venue only |
| **Irreversible** | send message/email, network POST, payment, external delete, print | **always requires human assent showing the actual payload** |

`optimus undo <ledger-seq>` replays compensations backwards. This is Achilles's DiffSandbox generalized from files to the OS, and it is what makes unattended operation defensible.

### 4.3 The Ledger is the only system of record

One signed, hash-chained, trust-labelled, append-only log. Memory, sessions, audit, replay, undo, cost accounting, the skill library's usage stats and the multiplayer view are **projections** over it, not separate stores. This merges Bellona's chain (which has no persistence) with Achilles's durable trust-labelled events (which have no chain), and retires Achilles's 22 independent SQLite databases as the source of truth.

Fixing the identity failure from [audit.md](audit.md) §2.3: the owner key lives **outside the gateway process** (DPAPI/TPM-backed, or the Windows agent identity), the gateway holds only the agent key, and `verify` takes an expected owner fingerprint as a **required** argument. A receipt that verifies against a key carried inside itself proves nothing.

### 4.4 Code-mode is the default tool interface

Given a mounted PowerShell surface of 10,000+ modules and an MCP gateway fronting dozens of servers, per-tool schemas in context are not viable — research.md §4.4 measures the alternative at **98.7% fewer tokens**. So:

- The gateway generates **typed façades** (Python/PowerShell stubs) from MCP tool schemas and cmdlet reflection.
- The model writes code against those façades in a **persistent runspace**, the way Prime Agent uses a persistent IPython kernel.
- Every façade call still mints a `CapabilityRequest` and receives a handle. **Code mode does not bypass the gate; it is a nicer way to ask it.**
- Three modes selected by a context-budget policy, not a config flag: direct schema (few tools) · deferred search (many) · code (chained work).

---

## 5. The Descent — the thing nobody ships

This is the answer to "ever-learning, ever-growing," made into a number.

```
run #1   tier 6/5   model drives GUI. 38,000 tokens, 94s, watched.
                    Ledger records: UIA path, screenshots, AND the API/COM/
                    HTTP calls observed underneath (the GOAL sniffer pattern).
   │
   ▼  GRADUATION ATTEMPT — offline, in an Agent Workspace, costs one deep-model call
candidate  tier 2   a parameterized PowerShell/COM/HTTP script synthesized from
                    the trace, with the task's post-conditions attached.
   │
   ▼  BATTERY — Bellona's Ludus + Achilles's post-condition verification
                    N cases · pass^k (ALL k trials must pass) · zero unsafe
                    attempts · runs in an isolated venue · never on live data
   │
   ▼  PROMOTION — only now does it become a registered tool
run #10  tier 2    0 model tokens. 400ms. Deterministic. Still gated, still
                    logged, still reversible.
   │
   ▼  DRIFT — post-condition fails after an app update
demote   tier 5    the skill is suspended, the GUI path re-learns, graduation
                    re-attempts. Apps change; the ladder must go both ways.
```

Every skill carries `tokens_saved_cumulative`. The system reports its own ROI, and the headline demo is a chart of cost-per-task falling toward zero on repeated work.

Three properties that make this ours rather than a reimplementation of GOAL/Skill-DisCo:

1. **Graduation is gated by the same verifier that gates everything else** — a skill is promoted the way a tool is promoted, with pass^k and a cost ceiling. The research proposes synthesis; nobody wires it to a promotion gate.
2. **Demotion exists.** Every published approach graduates and none of them fall back. That is why they are demos.
3. **It is accounted.** research.md §2.1: a 40× token spread between harnesses at equal pass rate, and nobody optimizes for it. We make the number go down on purpose and regression-test it.

This is also the honest answer to misevolution ([research.md](research.md) §4.8): self-improvement here can only ever produce a *candidate*, which must beat a frozen battery before it changes behaviour, and every promotion is a reviewable diff in the Ledger with one-command revert.

---

## 6. Watching it work — two venues, both watchable

**Watch mode** — the agent drives *your* desktop, in front of you.
- Every action is pre-announced: the target UIA element is highlighted for ~300ms with a one-line narration before the input is injected. This is the "in front of your eyes" property and it costs almost nothing.
- **Input arbitration:** a low-level hook watches for real human input. Any keystroke or mouse move **yields immediately** — pause, not abort — and the pane says "you have the controls." Resume is explicit. The agent never fights you for the cursor.
- Panic: a global hotkey hard-stops and offers `undo` of the whole run.
- Speed is deliberately capped here. Watch mode is for trust-building and for tasks on live data.

**Work mode** — the agent drives an **Agent Workspace** (Windows 2026: separate desktop, its own account, scoped authorization) or UFO2's PiP virtual desktop.
- Full speed, speculative multi-action batching on, your desktop untouched.
- The PiP surface is streamed into the Optimus watch pane, so you can still see it happen — you just aren't sharing a mouse with it.
- This is also the venue for graduation batteries: never on live data, never on your session.

The mode is a **policy attribute**, not a UI toggle: rules can say "irreversible verbs are Watch-mode only," or "any task touching `credential` targets runs in Watch with per-step assent."

---

## 7. Harvest from Bellona and Achilles

Per your instruction — no repair work, ideas only. Full detail in [audit.md](audit.md) §6.

**From Bellona (designs, essentially none of the code):** the single mandatory gate with audit-before-execution; CEL policy with structural deny-before-allow and named refusals; hash-chained ledger + Merkle root + standalone `verify`; registration ≠ exposure; **the Ludus battery** (nothing enters the registry without surviving trials — this becomes the Descent's promotion gate); **pass^k with a cost gate and distinct exit codes**; budget as a circuit breaker rather than a report; the signed-ticket shape for desktop input (gate-issued action id, freshness window, permanent replay memory).

**From Achilles (largely the code):** the trust-label plane, enforced at every authorize site; **DiffSandbox**, promoted to the default and generalized into the reversal journal; the advisor asymmetry (reviewers block, never authorize); action schema derived from the tool registry with recorded degradation; mutation-aware batching with one audit event per call; **the harness tournament** as the seed of the eval plane; AGENTS.md as untrusted document content; the honest-status documentation discipline; hardware-aware routing (VRAM fitting, dual brains, idle sleep, GPU arbiter, plan/act role split); the WSL2/OpenShell bridge.

---

## 8. Build order

Each milestone ends in something demonstrable. Nothing starts until the one before it is measured.

| # | Milestone | Done when |
|---|---|---|
| **M0** | **Skeleton + Ledger + Gate.** Handle-issuing gate, trust labels, CEL rules, signed chain with an out-of-process owner key, `verify` requiring a fingerprint. Harvest Achilles's trust plane. | An adversarial suite passes: path traversal, target substitution, forged receipt, tightened-law-after-park, redirect-to-private-space — all refused, all named in the log. |
| **M1** | **Mount the hands.** MCP gateway + Windows-MCP + PoshMCP + CDP-MCP behind the gate. Code-mode façades over a persistent runspace. | The agent opens Excel, edits a sheet, saves it — every action gated, logged, and undoable. |
| **M2** | **Venues + reversal.** Agent Workspace / PiP, WSL2, the reversal journal, `optimus undo`. | A destructive multi-app task runs in Work mode and is fully undone by one command. |
| **M3** | **Watch pane.** Narration, element highlight, input arbitration and yield, panic hotkey, PiP stream. | A non-technical person watches it book something and takes the controls mid-run without breaking it. |
| **M4** | **Verifier + tournament.** Post-conditions, batteries, pass^k gates, cost ceilings; Achilles's tournament generalized to score any mounted loop. | UFO2, Windows-MCP-direct, and our loop are scored on the same task set, with tokens-per-solved-task reported. |
| **M5** | **The Descent.** Trace capture with the API sniffer, graduation synthesis, promotion battery, demotion on drift, `tokens_saved` accounting. | A task costs ~38k tokens on run 1 and 0 on run 10, with the promotion diff reviewable and revertible. |
| **M6** | **Interop.** ACP client (Zed/JetBrains presence), AG-UI emitter, SKILL.md conformance, MCP server mode, OTel spans. | Optimus appears in Zed's agent registry and any AG-UI frontend drives it. |

M0–M2 is the credible open-source release. M5 is the reason anyone stars it.

---

## 9. What would kill this, honestly

- **Agent Workspace turns out to be gated or limited** (app compatibility in a second session, DRM, GPU access, licensing). Mitigation: UFO2's PiP is an independent fallback, and WSL2/VM remains for non-GUI work. Verify early — this is an M2 spike, not an assumption.
- **Local VLM grounding is too weak for tier 6.** Likely true today. Mitigation: the whole architecture is built to *avoid* tier 6 — it is the last resort by design, and every graduation moves work away from it. A weak vision tier degrades the system gracefully instead of breaking it.
- **Graduation fails on stateful GUI-only apps.** Expect it. The ladder is per-skill, so unlearnable tasks simply stay at tier 5 forever and cost what they cost.
- **Microsoft absorbs the layer.** Plausible. Our moat is the two things a platform vendor will not ship: **local-first and model-agnostic**, and an **open, auditable** governance plane that answers to the user rather than to Intune.
- **Scope.** Seven things to build is already ambitious. The mount table is load-bearing: every time we are tempted to write something on it, the answer is no.

---

## 10. The one-line version

**Windows just became an agent platform and shipped no conscience with it. Optimus is the layer that makes an OS-driving agent safe to watch, cheap to repeat, and impossible to lie about — and it gets faster every time you use it.**

---

*Next: M0 spike — the handle-issuing gate and the adversarial suite, plus an early feasibility probe on Agent Workspace so M2's assumption is tested before it is depended on.*

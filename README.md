# Optimus

**An agent harness where every effect is authorized once, recorded once, and metered — and the receipt verifies.**

[![tests](https://github.com/Priyam1713/optimus/actions/workflows/ci.yml/badge.svg)](https://github.com/Priyam1713/optimus/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![local-first](https://img.shields.io/badge/inference-local--first-green.svg)](configs/engines.toml)

Optimus runs coding agents against a signed, append-only ledger. Every file
write, every shell command, every model call passes through one authorization
path and lands as a hash-chained row you can verify afterwards against a key the
agent never had.

It is **local-first**: it drives llama.cpp, vLLM, SGLang or Ollama by default and
cannot reach a hosted API unless you explicitly opt in.

> **Status: early.** M0–M3 are built and tested. It runs complete Terminal-Bench
> trials end to end and has not yet solved one. [STATUS.md](STATUS.md) leads with
> what does not work and records 17 findings from building it — including four
> consecutive context-accounting bugs that a green test suite hid.

---

## Why this exists

Three numbers from [the research](docs/research.md) §2.1, measured across
harnesses on the same 50 Terminal-Bench tasks:

| harness | tokens / solved task | no-action turns / task |
|---|---:|---:|
| Goose | **28–37K** | 0.2–0.3 |
| OpenHands-SDK | ~841K | — |
| OpenCode | 1.1–1.5M | 2.0–2.16 |

Pass rates differed by 0–8 points. **The 20–40× spread is context accumulation
and idle turns, not intelligence** — and nobody publishes either number.

Optimus publishes both, plus two more that only a harness with a real
authorization layer can produce: *unsafe attempts refused* and *operator
interventions required*.

## What makes it different

**One authorization path.** Every effect mints a request and receives a
*handle* carrying a resolved target — an open fd, a validated argv — never a
string an executor re-parses. Executors accept nothing else, which removes path
traversal and target substitution as bug classes rather than defending against
them case by case.

**One system of record.** A hash-chained, Ed25519-signed event log. Memory,
audit, replay, undo and cost accounting are *projections* of it. When a trial
was killed mid-run and lost every metric, the entire receipt was rebuilt by
folding the ledger — because the ledger was never the copy.

**Verification needs a key the agent cannot hold.** `optimus verify` requires an
owner fingerprint supplied out of band. A chain that verifies only against the
keys it carries reports `UNATTESTED`, never `VALID`.

**Context that proves it kept what mattered.** Compaction is priority-ordered
and dependency-aware, and every compaction is *validated* — one that would drop
a safety constraint is refused and rolled back. No shipped harness does this;
the failure mode has a paper and no implementation.

**Autonomy is a signed document, not a flag.** An unattended run acts under an
owner-signed *envelope* naming one actor, one verb set, one venue, an action
ceiling and an expiry. The Gate can verify one and structurally cannot mint one.
There is no `--yolo`.

## Install

```bash
git clone https://github.com/Priyam1713/optimus && cd optimus
python -m venv .venv && .venv/bin/pip install -e ".[loop]"    # Windows: .venv\Scripts\pip
```

## Quickstart

No API key required. Point it at any OpenAI-compatible local server:

```bash
# 1. What can serve a turn, and what is refused
optimus engines --live

# 2. An owner key, kept off the agent's path
optimus keygen --out state/owner.key

# 3. See the whole path run end to end - gate, ledger, loop, attest, verify
python scripts/m3_demo.py
```

```
[1] owner key      fingerprint 7c4319fc91ad96ae
[2] envelope       env_23d99437135b2a64, 50 actions, venue demo-shell
[3] priming        88 chars of structure, one round trip
    run=demo-run stop=finished turns=7 tokens=13,230 (cache 8,400) no_action=1 denials=1
[4] verifier       file says 'CHANGELOG.md ok' -> solved=True
[5] ledger         28 rows; VALID chain=True signatures=True owner_match=True
```

## On Terminal-Bench

Optimus registers with [Harbor](https://github.com/harbor-framework/harbor), the
official Terminal-Bench 2.0 harness, as one `BaseAgent` subclass:

```bash
optimus envelope --owner state/owner.key --principal you \
                 --any-workspace --venue harbor --isolation CONTAINER

python scripts/bench.py --tasks 10          # local engines, no key, no cost
optimus report jobs/<job-id>
```

When a trial does something surprising, `optimus why` reads it back out of its
ledger — where the turns went, how the prompt grew, what the Gate refused, and
what stopped it:

```
$ optimus why jobs/<job-id>/<trial>
stopped  max_turns
verifier not solved
ledger   171 rows, 40 turns, envelope env_e586... used 40x

where the tokens went    617,363 total, 439,927 cached (80% of input)
  prompt size per turn, peak 26,888
   19 #####################...  23,860
   20 ########################  26,461  <- repeat
   21 ############............  13,729  <- compacted 18 episodes, 31288 -> 25830
```

`optimus report` publishes **pass^k alongside pass@k**. Harbor computes pass@k —
the chance at least one of k attempts succeeds, which rises as you buy more
attempts. pass^k asks whether *all* k succeed. One measures how cheap a lottery
ticket is; the other measures whether you would put the thing in a pipeline.

## Documentation

| | |
|---|---|
| [STATUS.md](STATUS.md) | What exists, what does not, and every finding from building it |
| [docs/apex.md](docs/apex.md) | The architecture: mount-vs-build decisions and the five composition invariants |
| [docs/research.md](docs/research.md) | The field survey the design is answering |
| [docs/audit.md](docs/audit.md) | An unsparing audit of two predecessor systems, and what was salvaged |
| [docs/architecture.md](docs/architecture.md) | The OS/desktop plane, attached at M6 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is next, and what is deliberately deferred |

## Design invariants

1. **One authorization path.** Every effect mints a capability request and
   receives a handle carrying a resolved target.
2. **One system of record.** The signed ledger. Everything else is a projection.
3. **Trust is provenance, and it never widens.** Untrusted content can shape
   *how* work is done and never widen *what may be done*.
4. **Reversibility is a declared type.** Policy keys on it; the inverse is
   written to the ledger *before* the act.
5. **Everything is metered.** A capability that cannot be metered cannot be
   promoted.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The house rule worth knowing up front:
**a plane that is correct in its own units is not correct.** Four separate
context bugs shipped past a green suite because each component measured
faithfully in a unit nothing downstream was billed in. Tests are necessary and
they are not sufficient — every milestone gets an end-to-end run.

## License

Apache-2.0. See [LICENSE](LICENSE).

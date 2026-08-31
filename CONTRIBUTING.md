# Contributing to Optimus

## Getting set up

```bash
python -m venv .venv
.venv/bin/pip install -e ".[loop,harbor,dev]"    # Windows: .venv\Scripts\pip
.venv/bin/pytest -q
```

The suite is fast (under a minute) and has no network or container
dependencies. Tests that need a POSIX shell or Harbor skip cleanly without them.

## The house rules

These are not style preferences. Each one exists because breaking it cost this
project a real, documented failure — see the findings in
[STATUS.md](STATUS.md).

### 1. A plane that is correct in its own units is not correct

Four separate context-accounting bugs shipped past a green test suite because
each component measured faithfully in a unit nothing downstream was billed in:
episode tokens against rendered tokens, a generic tokenizer against a server's
chat template, a ratio fitted at one scale and used at another, eviction sized
in the wrong denomination.

When a component reports a number that another component acts on, the unit is
part of the contract. Say it in the name, the docstring, or both.

### 2. Where a provider knows a number, that number is the truth

Billed tokens are read off the response, never computed. Prompt budgets are
anchored on the size the provider reported for the last call. If a provider does
not report something, the field stays zero — **an absent number is honest and an
estimated one contaminates the headline.**

### 3. Tests are necessary and not sufficient

Every milestone gets an end-to-end run against something real. The precedent:
115 tests passed while the harness silently lowercased every filename it
created. Running the demo caught it. Ten real Terminal-Bench runs caught four
more that 239 tests did not.

If you add a plane, add a script that exercises it for real.

### 4. Refuse rather than downgrade

A venue that cannot honestly provide the isolation asked for raises, it does not
quietly hand back something weaker. A compaction that would drop a safety
constraint is refused and rolled back. A broken policy rule denies. An engine
without a credential is excluded at selection time, loudly.

Silence is the failure mode this project is built against.

### 5. Never name a weak guarantee after a strong one

`gate/remote.py` sets `pins_identity = False` and says at length why, rather
than letting container-side resolution inherit the local plane's name. If your
component provides less than an existing one, the type, the field, or the
docstring has to say so.

### 6. The receipt is a projection, never a copy

Anything published must be derivable from the ledger. There is exactly one
receipt builder for precisely this reason: two of them is how a metric came to
exist only on the crash path.

## Documentation style

Docstrings explain *why*, and cite the finding or document that forced the
decision. A comment that restates the code is noise; one that records the bug
the code prevents is the most valuable line in the file.

## Pull requests

- Run `pytest -q` and say what it reported.
- If you fixed a bug a test did not catch, add the test **and** say in
  STATUS.md why the suite missed it.
- Report outcomes faithfully. If something is untested against real
  infrastructure, say so rather than implying coverage you do not have.

## Security

See [SECURITY.md](SECURITY.md). Do not open a public issue for a sandbox escape,
an authorization bypass, or a way to forge a ledger or an envelope.

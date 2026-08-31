# Security Policy

Optimus is an authorization layer for autonomous agents. A defect here is not a
crash — it is an agent doing something nobody permitted, or a receipt that says
otherwise.

## Reporting a vulnerability

Please use GitHub's [private vulnerability
reporting](https://github.com/Priyam1713/optimus/security/advisories/new) rather
than a public issue.

Report privately if you find a way to:

- **Escape target resolution** — reach a file, host or process outside the
  resolved workspace through traversal, a symlink or junction, a race, or a
  target the Gate resolved differently from the executor that received it.
- **Act without a handle** — cause any effect that did not pass through
  `Gate.submit`, or construct a `Handle` outside `gate/handle.py`.
- **Forge or widen an envelope** — get the Gate to accept an envelope that the
  owner key did not sign, or use one beyond its actor, verbs, venue, workspace,
  action ceiling or expiry.
- **Forge a ledger** — produce a chain that reports `VALID` against an owner
  fingerprint whose key did not sign it, or mutate a recorded event without
  `verify` reporting a failure.
- **Bypass a deny rule** — reach a target that `deny-sensitive-write` or the
  grader rules should have refused.
- **Widen trust** — get untrusted-origin material to authorize a mutation
  without an envelope or a human assent.

## What is already documented, and not a vulnerability

These are known, stated in [STATUS.md](STATUS.md), and reported honestly rather
than defended:

- **The remote plane pins nothing.** Inside a container the Gate resolves
  lexically and cannot capture an inode, so `pins_identity` is `False` and the
  TOCTOU window is a whole round trip. The container is the wall.
- **Policy does not constrain what `bash` does.** `deny-grader-script` matches
  shell text. It is labelled a tripwire in the code and is trivially evaded by
  anyone who means to; it catches the honest mistake.
- **Residual TOCTOU on Windows.** No `dir_fd`, no `O_NOFOLLOW`, so a
  microsecond window remains between the identity check and the open. Closing it
  needs `NtCreateFile` through a native module.

A report that one of these behaves as documented is not a vulnerability. A
report that one of them is *worse than documented* very much is.

## Handling secrets

Optimus never reads a provider credential into its own configuration. Engine
manifests carry the *name* of an environment variable, never a value, and
credentials are read at call time. No key is written to the ledger, the metrics
file, or any log.

`state/` and `*.env` are git-ignored. If you believe a credential has been
committed, treat it as compromised and rotate it — removing it from history does
not un-publish it.

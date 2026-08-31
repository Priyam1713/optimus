"""Targets that live in someone else's filesystem.

Everything in M0–M2 resolves against *this* machine: `resolve_fs` stats the path,
pins `(st_dev, st_ino)`, and `FileCapability` re-checks that identity at the
moment of open. That is the property the whole authorization story rests on, and
none of it is available across a container boundary. Harbor hands us an
environment we drive with `exec`; there is no host path to stat and no fd to
hold.

So this module states the weaker guarantee explicitly rather than reusing the
strong one's name:

* **Containment is lexical and total.** `..` is collapsed with no I/O — the same
  rule as `resolve_fs` step (1) — and a path that escapes the declared workspace
  is refused before anything is sent anywhere. This part is exactly as strong
  remotely as locally, because it never needed the filesystem.
* **Identity is not pinned, and `pins_identity` says so.** There is no remote
  inode to capture, so the TOCTOU window that `capability.py` narrows to
  microseconds is, here, the whole round trip. What bounds the damage is the
  venue: a Terminal-Bench container is disposable, and `Isolation.CONTAINER` is
  a real wall where a pinned inode was only ever a latch.
* **`exists` answers "not proven to exist"**, because a remote stat costs a round
  trip per resolution and would be stale by the time policy read it. A rule that
  keys on `target.exists` therefore fails in the closed direction remotely.

**Shell.** `WorkspaceResolver` refuses a shell string for `execute`, on the
grounds that accepting one hands quoting decisions to whoever wrote it. That rule
survives here, but a terminal benchmark is *made* of shell, so refusing outright
would just mean refusing the task. The compromise is that a shell request must
say so: the spec is `{"script": "..."}`, never a bare string, and the resolver —
not the model — builds `(shell, -lc, script)`. The model never composes an argv,
so it never makes a quoting decision; the script arrives as one opaque argument
and is recorded verbatim.

Be clear about what that does and does not buy. Policy can see `target.script`
and can pattern-match it, but a rule written that way is a **tripwire, not a
wall**: it catches the honest mistake and any adversary steps around it in one
substitution. Inside a container the wall is the container. What the Gate
contributes on this plane is provenance, metering, structural refusal of verbs
outside the envelope, and a verbatim record of every command — which is more than
any harness on the Terminal-Bench board publishes, and less than a sandbox.

Attribute names deliberately mirror `FsTarget` and `ArgvTarget` (`target.relpath`,
`target.program`, `target.argv`, `target.cwd`), so one policy document governs
both planes instead of two that drift.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Sequence

from .targets import ResolvedTarget, TargetRefused
from .types import CapabilityRequest, Verb

#: What the Gate is allowed to *claim*. Local capabilities set this True; nothing
#: on this plane may.
PINS_IDENTITY = False


def posix_norm(path: str) -> str:
    """Absolute, `..`-collapsed, POSIX. No filesystem access, ever.

    `posixpath.normpath` collapses `..` purely lexically, which is what step (1)
    of `resolve_fs` does before it is allowed to touch anything. Doing it without
    I/O is the point: a path that escapes is rejected before it can be probed.
    """
    if not path.startswith("/"):
        raise ValueError("posix_norm needs an absolute path")
    return posixpath.normpath(path)


def _within(child: str, parent: str) -> bool:
    """Containment by path components, not string prefix."""
    if child == parent:
        return True
    return child.startswith(parent.rstrip("/") + "/")


@dataclass(frozen=True, slots=True)
class RemoteFsTarget(ResolvedTarget):
    """A path inside another machine's filesystem. Contained, but not pinned."""

    path: str = ""
    workspace: str = ""
    #: Name of the venue that will carry the effect. Set by harness code.
    venue: str = ""

    #: There is no remote inode to capture. Stated as data so that a receipt
    #: records the weaker guarantee instead of implying the stronger one.
    pins_identity: bool = PINS_IDENTITY

    def attrs(self) -> dict[str, Any]:
        rel = self.path[len(self.workspace):].lstrip("/") if _within(
            self.path, self.workspace
        ) else self.path
        name = posixpath.basename(self.path)
        # `PurePosixPath.suffix` rather than a hand-rolled split, so `.env` has
        # no suffix here exactly as it has none on the local plane. Two planes
        # that answer the same question differently are two policies.
        return {
            "kind": self.kind,
            "path": self.path,
            "relpath": rel,
            "name": name,
            "suffix": PurePosixPath(name).suffix,
            # Not "it does not exist" — "its existence was not established".
            # A `must exist` rule therefore fails closed on this plane.
            "exists": False,
            "venue": self.venue,
            "pins_identity": self.pins_identity,
        }


@dataclass(frozen=True, slots=True)
class RemoteArgvTarget(ResolvedTarget):
    """One argv, in one remote directory. `script` is set only for shell work."""

    argv: tuple[str, ...] = ()
    cwd: str = ""
    #: The shell source, when this argv is a shell invocation the *resolver*
    #: built. Empty for a plain argv. Recorded verbatim.
    script: str = ""
    venue: str = ""
    pins_identity: bool = PINS_IDENTITY

    def attrs(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "program": self.argv[0] if self.argv else "",
            "argv": list(self.argv),
            "cwd": self.cwd,
            "script": self.script,
            "venue": self.venue,
            "pins_identity": self.pins_identity,
        }


@dataclass(frozen=True, slots=True)
class RemoteCapability:
    """What a remote executor holds.

    Deliberately not named `Capability`-something-that-verifies: there is nothing
    to re-verify. `verify()` exists to keep the venue protocol honest and to
    document, at the call site, that the check is empty here.
    """

    target: RemoteFsTarget | RemoteArgvTarget

    @property
    def argv(self) -> tuple[str, ...]:
        if not isinstance(self.target, RemoteArgvTarget):
            raise TargetRefused("not an argv target")
        return self.target.argv

    @property
    def cwd(self) -> str:
        return getattr(self.target, "cwd", "") or getattr(self.target, "workspace", "")

    @property
    def path(self) -> str:
        if not isinstance(self.target, RemoteFsTarget):
            raise TargetRefused("not a filesystem target")
        return self.target.path

    def verify(self) -> None:
        """No-op, and named so.

        Local `FileCapability._verify` re-checks containment and identity because
        it can. Across a container boundary there is nothing to re-check that is
        not another round trip returning something already stale, so this does
        nothing and says nothing — rather than performing a check that would read
        as a guarantee it is not.
        """
        return None


class RemoteResolver:
    """Resolves against one workspace inside a remote environment.

    Same shape as `WorkspaceResolver`, same refusals, weaker pinning.
    """

    def __init__(self, workspace: str, *, venue: str, shell: str = "bash"):
        self.workspace = posix_norm(workspace)
        self.venue = venue
        self.shell = shell

    def __call__(self, req: CapabilityRequest) -> ResolvedTarget:
        spec: Any = req.target_spec

        match req.verb:
            case Verb.READ | Verb.WRITE | Verb.DELETE:
                if not isinstance(spec, str):
                    raise TargetRefused(
                        f"{req.verb} needs a path string, got {type(spec).__name__}"
                    )
                return self.resolve_path(spec)

            case Verb.EXECUTE:
                return self.resolve_exec(spec)

            case _:
                raise TargetRefused(
                    f"the remote plane has no resolver for verb {req.verb}"
                )

    # -- the two resolutions --------------------------------------------------

    def resolve_path(self, spec: str) -> RemoteFsTarget:
        if not spec or "\x00" in spec:
            raise TargetRefused("empty or NUL-bearing path")
        joined = spec if spec.startswith("/") else posixpath.join(self.workspace, spec)
        lexical = posix_norm(joined)
        if not _within(lexical, self.workspace):
            raise TargetRefused(f"path escapes the workspace: {spec}")
        # One return, after the check — same discipline as `resolve_fs`, for the
        # same reason: Bellona's resolver returned early on one branch and that
        # branch was the bug (`audit.md` §2.1).
        return RemoteFsTarget(
            kind="remote_fs", path=lexical, workspace=self.workspace, venue=self.venue
        )

    def resolve_exec(self, spec: Any) -> RemoteArgvTarget:
        if isinstance(spec, str):
            raise TargetRefused(
                "execute needs an argv list or an explicit {'script': ...}; a bare "
                "string is ambiguous about whether a shell is involved"
            )
        if isinstance(spec, dict):
            script = spec.get("script")
            if not isinstance(script, str) or not script.strip():
                raise TargetRefused("{'script': ...} needs a non-empty string")
            if "\x00" in script:
                raise TargetRefused("NUL in script")
            cwd = spec.get("cwd") or self.workspace
            resolved_cwd = self.resolve_path(str(cwd)).path
            # The *resolver* builds the argv. The model supplied one opaque
            # argument and made no quoting decision.
            return RemoteArgvTarget(
                kind="remote_argv",
                argv=(self.shell, "-lc", script),
                cwd=resolved_cwd,
                script=script,
                venue=self.venue,
            )
        if isinstance(spec, Sequence):
            items = [str(a) for a in spec]
            if not items:
                raise TargetRefused("empty argv")
            if any("\x00" in a for a in items):
                raise TargetRefused("NUL in argv")
            if not items[0].strip():
                raise TargetRefused("empty program")
            return RemoteArgvTarget(
                kind="remote_argv",
                argv=tuple(items),
                cwd=self.workspace,
                venue=self.venue,
            )
        raise TargetRefused(f"cannot resolve execute spec of type {type(spec).__name__}")

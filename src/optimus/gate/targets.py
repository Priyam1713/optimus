"""Target resolution: turning a string the model wrote into something an
executor is allowed to touch.

This module exists because of `audit.md` §2.1 and §2.4. Bellona's gate authorised
a *URI string*, its executor received the model's *raw arguments*, and its path
resolver returned early — before the containment check — on exactly the branch
that matters (creating a file that does not exist yet). I reproduced an arbitrary
file write with three lines of input.

The rules here:

1. **Normalise lexically before touching the filesystem.** `..` is collapsed with
   no I/O, so a path that escapes is rejected before it can be probed.
2. **Re-check containment on every return path.** There is exactly one `return`
   in `resolve_fs` and it is after the last check.
3. **Resolve symlinks and re-check.** A junction inside the workspace pointing
   out of it is an escape.
4. **Pin what was resolved.** A `UrlTarget` carries the IPs that were checked, so
   the executor connects to those and DNS cannot change its mind afterwards.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from ..ledger.events import canonical


class TargetRefused(Exception):
    """Resolution failed. Always a refusal, never a fallback."""


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """What the Gate hands to an executor. Never a raw string from the model."""

    kind: str

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.attrs())).hexdigest()

    def attrs(self) -> dict[str, Any]:
        return {"kind": self.kind}


def identity_of(path: str) -> tuple[int, int] | None:
    """(device, inode) — the file the OS actually means, not the name.

    Windows populates `st_ino`/`st_dev` from the file index (3.8+), so this works
    on both platforms. Captured at resolve time and re-checked at open time, it
    is what turns "a path that was safe a moment ago" into "the same file".
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@dataclass(frozen=True, slots=True)
class FsTarget(ResolvedTarget):
    path: str = ""
    workspace: str = ""
    exists: bool = False
    #: Identity of the file at resolve time (None when it does not exist yet).
    identity: tuple[int, int] | None = None
    #: Identity of the directory the file lives in — what a create is anchored
    #: to, since the file itself has no identity to pin.
    parent_identity: tuple[int, int] | None = None

    def attrs(self) -> dict[str, Any]:
        p = Path(self.path)
        try:
            rel = p.relative_to(self.workspace).as_posix()
        except ValueError:  # pragma: no cover - resolve_fs guarantees containment
            rel = p.as_posix()
        return {
            "kind": self.kind,
            "path": p.as_posix(),
            "relpath": rel,
            "name": p.name,
            "suffix": p.suffix,
            "exists": self.exists,
            # True here and False on the remote plane (`gate/remote.py`): a
            # local capability re-checks (st_dev, st_ino) at open, a remote one
            # has nothing to re-check. Policy can therefore refuse to allow a
            # class of action on unpinned targets.
            "pins_identity": True,
        }


@dataclass(frozen=True, slots=True)
class ArgvTarget(ResolvedTarget):
    argv: tuple[str, ...] = ()
    cwd: str = ""

    def attrs(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "program": self.argv[0] if self.argv else "",
            "argv": list(self.argv),
            "cwd": self.cwd,
            "pins_identity": True,
        }


@dataclass(frozen=True, slots=True)
class UrlTarget(ResolvedTarget):
    url: str = ""
    scheme: str = ""
    host: str = ""
    port: int = 0
    #: Exactly the addresses that were checked. Executors MUST connect to these
    #: and set Host explicitly; re-resolving reopens DNS rebinding.
    pinned_ips: tuple[str, ...] = ()

    def attrs(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "ips": list(self.pinned_ips),
        }


@dataclass(frozen=True, slots=True)
class OpaqueTarget(ResolvedTarget):
    """For surfaces the Gate cannot inspect structurally yet — an MCP tool call,
    a UIA element, a COM moniker. Carries an identity and a namespace so policy
    can still speak about it."""

    namespace: str = ""
    identity: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def attrs(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "namespace": self.namespace,
            "identity": self.identity,
            **{f"detail.{k}": v for k, v in self.detail.items()},
        }


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------

def _norm(p: str | os.PathLike[str]) -> str:
    """Absolute, `..`-collapsed — and **case-preserving**.

    Case matters here and normalising it away is a real bug, not a cosmetic one:
    an agent asked to create `CHANGELOG.md` got `changelog.md` on disk, because
    `normcase` was applied to the path that was actually opened rather than only
    to the key used for comparison. That file is a different file to git and to
    any case-sensitive system downstream. Caught by running the thing, not by the
    tests — which is its own lesson.
    """
    return os.path.normpath(os.path.abspath(os.fspath(p)))


def _key(p: str) -> str:
    """Comparison form: case-folded where the platform folds case."""
    return os.path.normcase(p)


def _is_within(child: str, parent: str) -> bool:
    """Containment by path *components*, not string prefix.

    A prefix test says `C:\\work-secrets` is inside `C:\\work`. Bellona's resolver
    used a prefix test on URIs and its policy keyed on the resource kind that
    produced (`audit.md` §2.5).
    """
    c, p = _key(child), _key(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


def resolve_fs(spec: str, workspace: str | os.PathLike[str], *, must_exist: bool = False) -> FsTarget:
    if not spec or "\x00" in spec:
        raise TargetRefused("empty or NUL-bearing path")

    ws_real = _norm(os.path.realpath(workspace))
    if not os.path.isdir(ws_real):
        raise TargetRefused(f"workspace is not a directory: {workspace}")

    raw = Path(spec)
    joined = raw if raw.is_absolute() else Path(ws_real) / raw

    # (1) lexical normalisation, no filesystem access yet
    lexical = _norm(joined)
    if not _is_within(lexical, ws_real):
        raise TargetRefused(f"path escapes the workspace: {spec}")

    # (3) symlink/junction resolution of the deepest existing ancestor, then
    #     re-normalise the rejoined path and check containment again.
    probe = Path(lexical)
    tail: list[str] = []
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise TargetRefused(f"cannot anchor path inside the workspace: {spec}")
        tail.append(probe.name)
        probe = parent

    anchored = _norm(Path(os.path.realpath(probe)).joinpath(*reversed(tail)))
    if not _is_within(anchored, ws_real):
        raise TargetRefused(f"path escapes the workspace through a link: {spec}")

    exists = os.path.exists(anchored)
    if must_exist and not exists:
        raise TargetRefused(f"path does not exist: {spec}")

    # single return, after every check
    return FsTarget(
        kind="fs",
        path=anchored,
        workspace=ws_real,
        exists=exists,
        identity=identity_of(anchored) if exists else None,
        parent_identity=identity_of(os.path.dirname(anchored)),
    )


# --------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------

def resolve_argv(argv: Sequence[str], cwd: str | os.PathLike[str]) -> ArgvTarget:
    if not argv:
        raise TargetRefused("empty argv")
    items = [str(a) for a in argv]
    if any("\x00" in a for a in items):
        raise TargetRefused("NUL in argv")
    if not items[0].strip():
        raise TargetRefused("empty program")
    cwd_real = _norm(os.path.realpath(cwd))
    if not os.path.isdir(cwd_real):
        raise TargetRefused(f"cwd is not a directory: {cwd}")
    # No shell. A shell string is not an argv, and accepting one here would
    # hand quoting decisions to the model.
    return ArgvTarget(kind="argv", argv=tuple(items), cwd=cwd_real)


# --------------------------------------------------------------------------
# url
# --------------------------------------------------------------------------

def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Use the stdlib's classification rather than hand-rolled octet tests.

    Bellona hand-rolled this and also stripped ports with `rsplit(':')`, which
    keeps the *port* and discards the host (`audit.md` §2.7). `ipaddress` plus
    `urlsplit` removes both mistakes.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in ipaddress.ip_network("100.64.0.0/10")  # CGNAT
    )


def resolve_url(url: str, *, allow_private: bool = False) -> UrlTarget:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise TargetRefused(f"unsupported scheme: {parts.scheme or '(none)'}")
    host = parts.hostname  # urlsplit strips the port and the brackets correctly
    if not host:
        raise TargetRefused("no host in url")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise TargetRefused(f"dns resolution failed for {host}: {exc}") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise TargetRefused(f"{host} does not resolve")

    if not allow_private:
        for raw_ip in ips:
            ip = ipaddress.ip_address(raw_ip)
            if _ip_is_private(ip):
                raise TargetRefused(f"{host} resolves into private space ({ip})")

    return UrlTarget(
        kind="url",
        url=url,
        scheme=parts.scheme,
        host=host,
        port=port,
        pinned_ips=tuple(ips),
    )

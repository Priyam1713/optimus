"""What an executor actually holds: a capability, not a path.

`STATUS.md` carried this as a known residual after M0: a handle carried a
canonical path, so between the Gate resolving it and the executor opening it, the
name could come to mean a different file. Small window, real bug class.

**What this closes, precisely, because "TOCTOU-safe" is the kind of claim that
should never be made loosely:**

* The file's `(st_dev, st_ino)` is captured at resolve time and re-checked at
  open time. Swapping the file, or replacing it with a symlink to somewhere else,
  changes the identity and the open is refused.
* For a file that does not exist yet, the *parent directory's* identity is pinned
  instead, so a create cannot be redirected by swapping a directory underneath.
* Containment is re-verified at open time, not trusted from resolve time.
* On POSIX, `O_NOFOLLOW` additionally refuses to open a symlink at all.

**What remains open, honestly:** on Windows there is no `dir_fd` and no
`O_NOFOLLOW`, so between the identity check and the `open()` there is a window of
a few microseconds. Closing it fully needs `NtCreateFile` with
`FILE_OPEN_REPARSE_POINT` through a native module — a real M7 item, not something
to pretend is done. On POSIX the check-then-open is still not atomic either;
`dir_fd`-relative opens are the fix and are available where `os.supports_dir_fd`
says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import BinaryIO

from .targets import ArgvTarget, FsTarget, TargetRefused, _is_within, _norm, identity_of

_BINARY = getattr(os, "O_BINARY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class CapabilityViolation(Exception):
    """The world changed between authorisation and use. Never a warning."""


@dataclass(frozen=True, slots=True)
class FileCapability:
    """The right to touch one specific file, re-checked at the moment of use."""

    target: FsTarget

    @property
    def path(self) -> str:
        return self.target.path

    def _verify(self) -> None:
        # Containment first: cheapest check, and the one whose failure is worst.
        current = _norm(os.path.realpath(self.path)) if os.path.exists(self.path) else _norm(self.path)
        if not _is_within(current, self.target.workspace):
            raise CapabilityViolation(f"{self.path} no longer resolves inside the workspace")

        if self.target.exists:
            now = identity_of(self.path)
            if now is None:
                raise CapabilityViolation(f"{self.path} disappeared after authorisation")
            if now != self.target.identity:
                raise CapabilityViolation(
                    f"{self.path} is not the file that was authorised "
                    "(identity changed between resolve and open)"
                )
        else:
            parent = os.path.dirname(self.path)
            now = identity_of(parent)
            if now is None:
                raise CapabilityViolation(f"parent of {self.path} no longer exists")
            if self.target.parent_identity is not None and now != self.target.parent_identity:
                raise CapabilityViolation(
                    f"the directory holding {self.path} was replaced after authorisation"
                )

    def open_read(self) -> BinaryIO:
        self._verify()
        fd = os.open(self.path, os.O_RDONLY | _BINARY | _NOFOLLOW)
        return os.fdopen(fd, "rb")

    def open_write(self, *, truncate: bool = True) -> BinaryIO:
        self._verify()
        flags = os.O_WRONLY | os.O_CREAT | _BINARY | _NOFOLLOW
        flags |= os.O_TRUNC if truncate else os.O_APPEND
        if not self.target.exists:
            # A create must actually create. If something appeared at this path
            # after authorisation, that is a different file than the one allowed.
            flags |= os.O_EXCL
        fd = os.open(self.path, flags, 0o600)
        return os.fdopen(fd, "wb")

    def read_text(self, encoding: str = "utf-8") -> str:
        with self.open_read() as fh:
            return fh.read().decode(encoding, errors="replace")

    def write_text(self, text: str, encoding: str = "utf-8") -> int:
        data = text.encode(encoding)
        with self.open_write() as fh:
            return fh.write(data)

    def read_bytes(self) -> bytes:
        with self.open_read() as fh:
            return fh.read()

    def unlink(self) -> None:
        self._verify()
        os.unlink(self.path)

    def ensure_parent(self) -> None:
        """Create missing parents *inside* the workspace, then re-pin.

        Directory creation is a mutation, so it happens here — after the Gate
        allowed the write — rather than during resolution, which must stay
        side-effect free.
        """
        parent = os.path.dirname(self.path)
        if os.path.isdir(parent):
            return
        if not _is_within(_norm(parent), self.target.workspace):
            raise CapabilityViolation(f"refusing to create {parent} outside the workspace")
        os.makedirs(parent, exist_ok=True)
        object.__setattr__(
            self, "target",
            FsTarget(
                kind=self.target.kind, path=self.target.path, workspace=self.target.workspace,
                exists=self.target.exists, identity=self.target.identity,
                parent_identity=identity_of(parent),
            ),
        )


@dataclass(frozen=True, slots=True)
class ArgvCapability:
    """The right to run one specific argv in one specific directory."""

    target: ArgvTarget

    @property
    def argv(self) -> tuple[str, ...]:
        return self.target.argv

    @property
    def cwd(self) -> str:
        return self.target.cwd

    def verify(self) -> None:
        if not os.path.isdir(self.cwd):
            raise CapabilityViolation(f"cwd {self.cwd} no longer exists")


def capability_for(target: object) -> FileCapability | ArgvCapability:
    """Wrap a resolved target in the capability an executor can use."""
    if isinstance(target, FsTarget):
        return FileCapability(target)
    if isinstance(target, ArgvTarget):
        return ArgvCapability(target)
    raise TargetRefused(f"no capability for target kind {type(target).__name__}")

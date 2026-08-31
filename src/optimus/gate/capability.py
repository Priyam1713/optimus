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
* **On Windows, the same and better** — see `winfile.py`. The file is opened
  with `FILE_FLAG_OPEN_REPARSE_POINT`, which is the `O_NOFOLLOW` equivalent, and
  then identity *and* containment are verified on the **handle** rather than on
  the path. Once the handle is held the name cannot be re-pointed underneath it,
  so the object checked is necessarily the object read.

**What remains open, honestly:** an *intermediate* directory replaced between
the resolve and the open is still not caught on either platform. `O_NOFOLLOW`
only ever applied to the final component, and the Windows path has the same
shape; closing it needs component-by-component relative opens (`dir_fd` /
`openat` on POSIX, `NtCreateFile` with `OBJECT_ATTRIBUTES.RootDirectory` on
Windows). What bounds it now is that Windows reports where the handle actually
landed, so a redirected open is refused after the fact rather than honoured —
nothing is read through it, and a create that lands wrong is removed again.
POSIX does not yet do that much and should.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import BinaryIO

from . import winfile
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

    def _contains(self, real_path: str) -> bool:
        """Is the place a handle actually landed inside the workspace?"""
        return _is_within(_norm(real_path), self.target.workspace)

    def open_read(self) -> BinaryIO:
        self._verify()
        if winfile.available():
            fd = winfile.open_nofollow(
                self.path, write=False,
                expect_identity=self.target.identity if self.target.exists else None,
                contains=self._contains,
            )
            return os.fdopen(fd, "rb")
        fd = os.open(self.path, os.O_RDONLY | _BINARY | _NOFOLLOW)
        return os.fdopen(fd, "rb")

    def open_write(self, *, truncate: bool = True) -> BinaryIO:
        self._verify()
        if winfile.available():
            # Windows has no O_NOFOLLOW and no O_TRUNC that can be combined with
            # it, so the sequence is deliberately open -> verify -> truncate. A
            # truncating *create* would destroy the contents before anything had
            # been checked, which on a redirected path means destroying the
            # wrong file and only then noticing.
            fd = winfile.open_nofollow(
                self.path, write=True,
                create_new=not self.target.exists,
                expect_identity=self.target.identity if self.target.exists else None,
                contains=self._contains,
            )
            if truncate:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
            else:
                os.lseek(fd, 0, os.SEEK_END)
            return os.fdopen(fd, "wb")

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

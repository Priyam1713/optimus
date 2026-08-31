"""Opening a file on Windows without being redirected between check and open.

`STATUS.md` has carried this as a known residual since M1, and
`capability.py` says it plainly: POSIX gets `O_NOFOLLOW`, Windows gets nothing,
so between the identity check and the `open()` there is a window in which a
symlink or junction can be dropped in and the open follows it somewhere else.

## Why this does not use `NtCreateFile`

Because it does not need to, and the documented path is better.

The roadmap said this needed "`NtCreateFile` with `FILE_OPEN_REPARSE_POINT`
through a native module". Reading the actual API contracts changed the design in
two ways worth recording:

1. **`CreateFileW` already takes `FILE_FLAG_OPEN_REPARSE_POINT` (0x00200000)**,
   documented as "normal reparse point processing will not occur". That is the
   `O_NOFOLLOW` equivalent, on a stable Win32 API, reachable from `ctypes`. The
   `Nt*` family is the undocumented layer underneath, and reaching for it when
   the supported call does the job trades a stability guarantee for nothing.

2. **The real fix is not the flag, it is *where* the check happens.** The
   existing code verifies identity on a *path* and then opens that path — two
   operations against a name that something else can re-point in between. Here
   the file is opened first and verified **on the handle**: the volume serial
   and file id are read back with `GetFileInformationByHandleEx`, and the
   canonical path with `GetFinalPathNameByHandleW`. Once the handle is held the
   name cannot be re-pointed underneath it, so the object that was checked is
   necessarily the object that will be read. Open-then-verify closes the window
   that check-then-open leaves open, and it does so without any native module.

Microsoft's own documentation is explicit that this identity is the right one:
"the identifier (low and high parts) and the volume serial number uniquely
identify a file on a single computer."

## What this closes, and what it does not

**Closed:** the final path component. A symlink, junction or any other reparse
point substituted at the target path is refused rather than followed, and a file
swapped for a different file is caught because the identity is read off the
handle that was actually opened.

**Caught but not prevented:** an *intermediate* directory replaced between the
resolve and the open. The file at the end of it is a genuine file and the open
succeeds; what refuses it is the containment check against the path
`GetFinalPathNameByHandleW` says the handle actually landed on. The read never
happens, and a create that lands wrong is removed again. *Preventing* it needs
component-by-component relative opens — `openat` on POSIX,
`OBJECT_ATTRIBUTES.RootDirectory` on Windows — and this file does not attempt
that. POSIX in this codebase has the same gap and does not even catch it:
`O_NOFOLLOW` only ever applied to the last component.

This is not a theoretical boundary. It was measured, by disabling this module
and running the same attack: an intermediate junction swapped inside the window
read back the contents of a file outside the workspace, with no refusal. That
counterfactual is what `tests/test_m7_winfile.py` exists to hold, and it is also
what discarded the first draft of the test — a junction at the *final* component
refused with or without this module, because a directory junction cannot be
opened as a file either way. A security test that passes both ways proves
nothing.

That distinction is the whole reason this module exists rather than a claim in a
document (house rule 5).

## Identity units

`GetFileInformationByHandleEx(FileIdInfo)` returns exactly what `os.stat`
reports as `st_dev` and `st_ino` on this platform — verified rather than
assumed, because the obvious candidate is wrong: `BY_HANDLE_FILE_INFORMATION`'s
32-bit `dwVolumeSerialNumber` does **not** equal `st_dev`, which CPython takes
from the 64-bit `FILE_ID_INFO`. Comparing the plausible one would have refused
every open on a correct file. Same lesson as `STATUS.md` M3-13..16, in a
different layer: two numbers that mean the same thing in different units.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass

__all__ = ["WindowsOpenRefused", "available", "open_nofollow"]

_IS_WINDOWS = sys.platform == "win32"


class WindowsOpenRefused(OSError):
    """The open was refused. Never a warning, and never downgraded to one.

    Subclasses `OSError` deliberately. Callers of `FileCapability` should not
    have to learn a Windows-specific exception to handle "the file was not what
    was authorised" — on POSIX that condition arrives as `OSError` from
    `O_NOFOLLOW`, and it should arrive as the same family here. A refusal that
    needs a platform check at every call site is a refusal that will eventually
    be missed at one of them.
    """


if _IS_WINDOWS:  # pragma: no cover - the import itself is platform-gated
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # -- constants, from the Win32 headers -----------------------------------
    GENERIC_READ = 0x8000_0000
    GENERIC_WRITE = 0x4000_0000
    #: Permissive on purpose: POSIX does not lock on open, and a share mode that
    #: excluded other readers would change this harness's behaviour rather than
    #: only its safety.
    FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    #: "Normal reparse point processing will not occur." Cannot be combined with
    #: CREATE_ALWAYS, which is why a truncating open here is OPEN_EXISTING
    #: followed by an explicit truncate rather than a truncating create.
    FILE_FLAG_OPEN_REPARSE_POINT = 0x0020_0000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0000_0400
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    #: FILE_INFO_BY_HANDLE_CLASS::FileIdInfo
    _FILE_ID_INFO = 18
    _VOLUME_NAME_DOS = 0x0
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183

    class _FILE_ID_128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FILE_ID_INFO_S(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FILE_ID_128),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]


def available() -> bool:
    """True when the no-follow path can be used at all."""
    return _IS_WINDOWS


@dataclass(frozen=True, slots=True)
class HandleFacts:
    """What the OS says about the object actually opened."""

    identity: tuple[int, int]
    is_reparse_point: bool
    final_path: str


def _facts(handle: int) -> HandleFacts:  # pragma: no cover - Windows only
    basic = _BY_HANDLE_FILE_INFORMATION()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(basic)):
        raise WindowsOpenRefused(
            f"could not read information for the opened handle "
            f"(error {ctypes.get_last_error()})"
        )

    ids = _FILE_ID_INFO_S()
    if not _kernel32.GetFileInformationByHandleEx(
        handle, _FILE_ID_INFO, ctypes.byref(ids), ctypes.sizeof(ids)
    ):
        raise WindowsOpenRefused(
            f"could not read the file id for the opened handle "
            f"(error {ctypes.get_last_error()})"
        )
    # Little-endian low 64 bits, which is what CPython reports as st_ino.
    file_id = int.from_bytes(bytes(ids.FileId.Identifier)[:8], "little")

    size = _kernel32.GetFinalPathNameByHandleW(handle, None, 0, _VOLUME_NAME_DOS)
    if size == 0:
        raise WindowsOpenRefused(
            f"could not resolve the final path of the opened handle "
            f"(error {ctypes.get_last_error()})"
        )
    buf = ctypes.create_unicode_buffer(size)
    written = _kernel32.GetFinalPathNameByHandleW(
        handle, buf, size, _VOLUME_NAME_DOS
    )
    if written == 0:
        raise WindowsOpenRefused("could not resolve the final path of the handle")
    final = buf.value
    # `\\?\C:\x` is the same place as `C:\x`; the prefix only lifts MAX_PATH.
    # A UNC final path keeps its own prefix, which is a different root and must
    # not be silently rewritten into a local one.
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]

    return HandleFacts(
        identity=(ids.VolumeSerialNumber, file_id),
        is_reparse_point=bool(
            basic.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT
        ),
        final_path=final,
    )


def open_nofollow(
    path: str,
    *,
    write: bool,
    create_new: bool = False,
    expect_identity: tuple[int, int] | None = None,
    contains: object | None = None,
) -> int:
    """Open `path` without following a reparse point, verifying on the handle.

    Returns a file descriptor the caller owns; closing it closes the underlying
    Windows handle. Raises `WindowsOpenRefused` for every way of not getting
    exactly the authorised file.

    `contains` is a predicate taking the handle's real path and returning
    whether it is acceptable. It runs *after* the open, against the path the
    handle actually landed on, which is the only containment check that cannot
    be raced.
    """
    if not _IS_WINDOWS:  # pragma: no cover - guarded by callers
        raise WindowsOpenRefused("this path is Windows-only")

    access = GENERIC_WRITE if write else GENERIC_READ
    if write and not create_new:
        # A truncating open would be CREATE_ALWAYS, which the documentation says
        # cannot be combined with FILE_FLAG_OPEN_REPARSE_POINT — and which would
        # destroy the contents *before* anything had been verified. Open, check,
        # and let the caller truncate afterwards.
        access |= GENERIC_READ
    disposition = CREATE_NEW if create_new else OPEN_EXISTING

    handle = _kernel32.CreateFileW(
        path, access, FILE_SHARE_ALL, None, disposition,
        FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if create_new and err in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
            # Something appeared at this path after authorisation. That is a
            # different file than the one allowed, which is exactly what O_EXCL
            # means on the other platform — so it raises exactly what O_EXCL
            # raises there, rather than inventing a platform-specific name for a
            # condition that already has a portable one.
            raise FileExistsError(
                f"{path} already exists; it was authorised as a new file"
            )
        raise WindowsOpenRefused(f"could not open {path} (error {err})")

    created = create_new
    try:
        facts = _facts(handle)

        if facts.is_reparse_point:
            # The O_NOFOLLOW equivalent. The handle refers to the link itself
            # rather than its target, so nothing has been read through it — but
            # a link is not the file that was authorised.
            raise WindowsOpenRefused(
                f"{path} is a reparse point (symlink or junction); refusing to "
                "open it, and refusing to follow it"
            )

        if contains is not None and not contains(facts.final_path):
            raise WindowsOpenRefused(
                f"{path} resolves to {facts.final_path}, which is outside the "
                "workspace it was authorised against"
            )

        if expect_identity is not None and facts.identity != expect_identity:
            raise WindowsOpenRefused(
                f"{path} is not the file that was authorised "
                "(identity changed between resolve and open)"
            )
    except BaseException:
        _kernel32.CloseHandle(handle)
        if created:
            # Do not leave debris from a create that was then refused.
            with contextlib.suppress(OSError):
                os.unlink(path)
        raise

    flags = os.O_RDONLY if not write else os.O_RDWR
    try:
        fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
    except OSError as exc:
        _kernel32.CloseHandle(handle)
        raise WindowsOpenRefused(f"could not adopt the handle for {path}: {exc}") from exc
    # From here the CRT owns the handle; closing the fd closes it, and calling
    # CloseHandle as well would be a double free.
    return fd

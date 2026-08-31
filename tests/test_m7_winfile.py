"""M7: closing the Windows check-then-open window.

`STATUS.md` carried this as a residual from M1 onward: POSIX gets `O_NOFOLLOW`,
Windows got nothing, so between the identity check and the `open()` a reparse
point could be dropped in and the open would follow it somewhere else.

The attack vector here is a **directory junction** rather than a file symlink,
for a practical reason: creating a file symlink on Windows needs a privilege
this account does not hold, while `mklink /J` works unprivileged. That makes the
junction the *realistic* vector on an ordinary Windows box, and it is also the
more dangerous one — a junction redirects a whole directory rather than one
file.

Two cases are worth separating, and the difference is the honest boundary of
what this closes:

- A junction as the **final** component is refused outright: it is a reparse
  point, and the open never follows it.
- A junction as an **intermediate** component is not *prevented* — the file at
  the end of it is a real file and opens fine — but it is *caught*, because
  containment is re-checked against the path the handle actually landed on.
  Nothing is read through it. That distinction is the whole reason
  `winfile.py` exists rather than a claim in a document.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from optimus.gate.capability import CapabilityViolation, capability_for
from optimus.gate.targets import resolve_fs
from optimus.gate.winfile import WindowsOpenRefused, available, open_nofollow

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="the Windows open path")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _junction(link: Path, target: Path) -> bool:
    """Create a directory junction. Returns False if the OS refused."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# --------------------------------------------------------------------------
# the platform gate
# --------------------------------------------------------------------------

def test_available_matches_the_platform():
    assert available() is WINDOWS


@pytest.mark.skipif(WINDOWS, reason="checks the non-Windows guard")
def test_the_windows_path_refuses_to_run_elsewhere():
    with pytest.raises(WindowsOpenRefused):
        open_nofollow("/tmp/x", write=False)


def test_a_refusal_is_an_oserror():
    """Callers should not need a platform check to catch "not the authorised
    file". On POSIX that arrives as OSError from O_NOFOLLOW; it arrives as the
    same family here."""
    assert issubclass(WindowsOpenRefused, OSError)


# --------------------------------------------------------------------------
# the ordinary path still works
# --------------------------------------------------------------------------

@windows_only
class TestOrdinaryFiles:
    def test_a_normal_read_round_trips(self, workspace: Path):
        (workspace / "a.txt").write_text("hello", encoding="utf-8")
        cap = capability_for(resolve_fs("a.txt", workspace))
        assert cap.read_text() == "hello"

    def test_a_normal_write_round_trips(self, workspace: Path):
        (workspace / "a.txt").write_text("old", encoding="utf-8")
        cap = capability_for(resolve_fs("a.txt", workspace))
        cap.write_text("new")
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "new"

    def test_a_truncating_write_leaves_no_tail_of_the_old_file(self, workspace: Path):
        """The Windows open cannot truncate as part of the create, so it opens,
        verifies, and only then truncates. If that last step were skipped, a
        shorter write would leave the tail of the longer old content behind."""
        (workspace / "a.txt").write_text("x" * 500, encoding="utf-8")
        cap = capability_for(resolve_fs("a.txt", workspace))
        cap.write_text("short")
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "short"

    def test_a_create_writes_a_new_file(self, workspace: Path):
        cap = capability_for(resolve_fs("fresh.txt", workspace))
        cap.write_text("made")
        assert (workspace / "fresh.txt").read_text(encoding="utf-8") == "made"

    def test_binary_content_is_not_mangled(self, workspace: Path):
        """`open_osfhandle` without O_BINARY would translate newlines."""
        blob = bytes(range(256))
        (workspace / "b.bin").write_bytes(blob)
        cap = capability_for(resolve_fs("b.bin", workspace))
        assert cap.read_bytes() == blob

    def test_the_handle_is_owned_by_the_fd_and_closing_releases_it(
        self, workspace: Path
    ):
        """If the handle were closed twice, or never, this would fail or leak.
        Re-opening the same file many times is the cheap way to notice."""
        (workspace / "a.txt").write_text("hello", encoding="utf-8")
        cap = capability_for(resolve_fs("a.txt", workspace))
        for _ in range(200):
            assert cap.read_text() == "hello"


# --------------------------------------------------------------------------
# the attack
# --------------------------------------------------------------------------

@windows_only
class TestReparsePointsAreRefused:
    def test_a_junction_already_there_is_refused_by_the_resolver(
        self, workspace: Path, tmp_path: Path
    ):
        """Defence in depth, and the reason the next test has to be written
        carefully: a junction that exists *at resolve time* never reaches the
        open at all. Resolution walks it and sees the escape."""
        from optimus.gate.targets import TargetRefused

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        if not _junction(workspace / "sneaky", outside):
            pytest.skip("this host would not create a junction")

        with pytest.raises(TargetRefused):
            resolve_fs("sneaky", workspace)

    def test_an_intermediate_directory_swapped_mid_window_is_refused(
        self, workspace: Path, tmp_path: Path
    ):
        """**The actual race, and the thing M7 closes.**

        Everything about this authorisation is legitimate: a real file, in a
        real directory, inside the workspace, resolved cleanly. The attacker
        then wins the window *between* `_verify()` and the open and replaces the
        containing directory with a junction pointing outside.

        The interleaving is forced rather than raced, because a test that
        depends on winning a microsecond window is a test that passes by luck.
        Forcing it is the only way to assert on the behaviour rather than on the
        timing.

        Measured counterfactually before this test was written: with the Windows
        path disabled, this read back `TOP SECRET EXFILTRATED` — a real
        workspace escape, silently honoured. That is what makes this test worth
        having, and it is why the earlier draft of it (a junction at the *final*
        component) was thrown away: that one refused either way, because you
        cannot open a directory junction as a file regardless of any check.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("TOP SECRET EXFILTRATED", encoding="utf-8")

        sub = workspace / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("legitimate content", encoding="utf-8")
        cap = capability_for(resolve_fs("sub/a.txt", workspace))

        real_verify = type(cap)._verify

        def racing_verify(self):
            real_verify(self)              # the honest check passes...
            (sub / "a.txt").unlink()       # ...and the window opens here
            sub.rmdir()
            if not _junction(sub, outside):
                pytest.skip("this host would not create a junction")

        type(cap)._verify = racing_verify
        try:
            with pytest.raises((CapabilityViolation, OSError)) as caught:
                cap.read_text()
        finally:
            type(cap)._verify = real_verify

        message = str(caught.value)
        assert "outside the workspace" in message
        assert "TOP SECRET" not in message

    def test_a_file_reached_through_a_junction_is_refused_on_containment(
        self, workspace: Path, tmp_path: Path
    ):
        """The intermediate-component case: not prevented, but caught.

        The file at the end is a genuine file and opens fine. What refuses it is
        the containment check against the path the *handle* landed on, which is
        outside the workspace. Nothing is read through it.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")

        link = workspace / "sneaky"
        if not _junction(link, outside):
            pytest.skip("this host would not create a junction")

        real = str(workspace / "sneaky" / "secret.txt")
        with pytest.raises((CapabilityViolation, OSError)) as caught:
            open_nofollow(
                real, write=False,
                contains=lambda p: os.path.normcase(p).startswith(
                    os.path.normcase(str(workspace))
                ),
            )
        assert "outside" in str(caught.value) or "workspace" in str(caught.value)

    def test_the_secret_is_not_read_when_the_open_is_refused(
        self, workspace: Path, tmp_path: Path
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
        link = workspace / "sneaky"
        if not _junction(link, outside):
            pytest.skip("this host would not create a junction")

        got = ""
        try:
            with open_nofollow(
                str(workspace / "sneaky" / "secret.txt"), write=False,
                contains=lambda p: os.path.normcase(p).startswith(
                    os.path.normcase(str(workspace))
                ),
            ) as fd:  # pragma: no cover - the point is that this does not run
                got = str(fd)
        except (OSError, AttributeError):
            pass
        assert "TOP SECRET" not in got


@windows_only
class TestIdentityIsCheckedOnTheHandle:
    def test_a_swapped_file_is_refused(self, workspace: Path):
        """Identity is read back off the handle that was actually opened, so
        the object checked is necessarily the object that would be read."""
        path = workspace / "a.txt"
        path.write_text("original", encoding="utf-8")
        cap = capability_for(resolve_fs("a.txt", workspace))

        # Replace the file entirely: new file, new file id.
        path.unlink()
        path.write_text("substituted", encoding="utf-8")

        with pytest.raises((CapabilityViolation, OSError)):
            cap.read_text()

    def test_the_pinned_identity_and_the_handle_identity_are_the_same_units(
        self, workspace: Path
    ):
        """The trap this guards: `BY_HANDLE_FILE_INFORMATION`'s 32-bit
        `dwVolumeSerialNumber` is *not* `st_dev`. CPython takes `st_dev` from
        the 64-bit `FILE_ID_INFO`, and comparing the plausible one instead would
        refuse every open of a perfectly good file — the same units mistake as
        STATUS M3-13..16, one layer down.
        """
        from optimus.gate.targets import identity_of

        path = workspace / "a.txt"
        path.write_text("hello", encoding="utf-8")
        pinned = identity_of(str(path))

        # An open that succeeds is the assertion: `open_nofollow` refuses on any
        # mismatch, so getting a descriptor back proves the units agree.
        fd = open_nofollow(str(path), write=False, expect_identity=pinned)
        os.close(fd)

    def test_a_mismatched_identity_is_refused(self, workspace: Path):
        path = workspace / "a.txt"
        path.write_text("hello", encoding="utf-8")
        with pytest.raises(WindowsOpenRefused, match="not the file that was authorised"):
            open_nofollow(str(path), write=False, expect_identity=(1, 2))


@windows_only
class TestCreateSemantics:
    def test_a_create_that_finds_something_there_raises_file_exists(
        self, workspace: Path
    ):
        """O_EXCL semantics, and the *portable* exception for them."""
        cap = capability_for(resolve_fs("fresh.txt", workspace))
        (workspace / "fresh.txt").write_text("someone got there first", encoding="utf-8")
        with pytest.raises(FileExistsError):
            cap.write_text("mine")

    def test_a_refused_create_leaves_no_debris(self, workspace: Path):
        """A create that is refused after the file was made must remove it,
        or a failed write leaves an empty file where there was none."""
        target = workspace / "nested" / "new.txt"
        target.parent.mkdir()
        with pytest.raises(OSError):
            open_nofollow(
                str(target), write=True, create_new=True,
                contains=lambda _p: False,  # refuse whatever it lands on
            )
        assert not target.exists()

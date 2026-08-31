"""The local venue: a killable process tree with a scrubbed environment."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time

from ..gate.capability import ArgvCapability
from .base import Isolation, VenueRequest, VenueResult, scrub_env, truncate

_WINDOWS = sys.platform == "win32"


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child *and its descendants*.

    `Popen.kill()` alone leaves grandchildren running — a build that spawned a
    compiler keeps the workspace locked long after the agent moved on. Bellona's
    timeout did not even kill the child (`audit.md` §2.12); this is the version
    that actually reaps.
    """
    if proc.poll() is not None:
        return
    if _WINDOWS:
        # taskkill walks the tree; the fallback matters when the pid is already
        # gone between the poll and the call.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


class LocalVenue:
    """Runs on this machine, in this filesystem, with a filtered environment.

    Honest about what it is: `PROCESS` isolation. It bounds runtime and reaps the
    tree; it does not give the command its own filesystem or network. Ask for
    `CONTAINER` and you will get a refusal from `choose()`, not this.
    """

    name = "local"

    def available(self) -> bool:
        return True

    def isolation(self) -> Isolation:
        return Isolation.PROCESS

    def run(self, cap: ArgvCapability, request: VenueRequest) -> VenueResult:
        cap.verify()
        argv = list(cap.argv)
        program = shutil.which(argv[0], path=scrub_env().get("PATH"))
        if program is None:
            return VenueResult(
                exit_code=127, stdout="", stderr=f"{argv[0]}: not found on PATH",
                venue=self.name, isolation=self.isolation(),
            )
        argv[0] = program

        popen_kwargs: dict = {
            "cwd": cap.cwd,
            "env": scrub_env(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            # Bytes, decoded once at the end: a text-mode pipe would decode
            # incrementally and can split a multi-byte character across reads.
            "text": False,
        }
        if _WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        started = time.monotonic()
        timed_out = False
        proc = subprocess.Popen(argv, **popen_kwargs)
        try:
            out, err = proc.communicate(timeout=request.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            out, err = proc.communicate()
        duration = int((time.monotonic() - started) * 1000)

        limit = request.max_output_bytes
        return VenueResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=truncate(out.decode("utf-8", errors="replace"), limit),
            stderr=truncate(err.decode("utf-8", errors="replace"), limit)
            + ("\n[venue] timed out and the process tree was killed" if timed_out else ""),
            venue=self.name,
            isolation=self.isolation(),
            timed_out=timed_out,
            duration_ms=duration,
        )


class WslVenue(LocalVenue):
    """Runs inside WSL2 — a different kernel, so a real namespace boundary.

    Achilles proved this bridge works and it is the cheapest real isolation
    available on a Windows workstation (`audit.md` §3.7).
    """

    name = "wsl"

    def available(self) -> bool:
        if not _WINDOWS or shutil.which("wsl.exe") is None:
            return False
        try:
            r = subprocess.run(["wsl.exe", "-e", "true"], capture_output=True, timeout=20, check=False)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def isolation(self) -> Isolation:
        return Isolation.CONTAINER

    def run(self, cap: ArgvCapability, request: VenueRequest) -> VenueResult:
        wrapped = ArgvCapability(
            type(cap.target)(
                kind=cap.target.kind,
                argv=("wsl.exe", "-e", *cap.argv),
                cwd=cap.target.cwd,
            )
        )
        result = super().run(wrapped, request)
        result.venue = self.name
        result.isolation = self.isolation()
        return result


class DockerVenue(LocalVenue):
    """Runs in a container with the workspace bind-mounted."""

    name = "docker"

    def __init__(self, image: str = "python:3.12-slim"):
        self.image = image

    def available(self) -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def isolation(self) -> Isolation:
        return Isolation.CONTAINER

    def run(self, cap: ArgvCapability, request: VenueRequest) -> VenueResult:
        flags = ["--rm", "-w", "/work", "-v", f"{cap.cwd}:/work"]
        if not request.allow_network:
            flags += ["--network", "none"]
        if not request.writable:
            flags += ["--read-only"]
        wrapped = ArgvCapability(
            type(cap.target)(
                kind=cap.target.kind,
                argv=("docker", "run", *flags, self.image, *cap.argv),
                cwd=cap.target.cwd,
            )
        )
        result = super().run(wrapped, request)
        result.venue = self.name
        result.isolation = self.isolation()
        return result

"""The tool plane for a workspace that is not on this machine.

`GatedTools` is the local plane: it holds an fd, re-pins an inode, and opens with
`O_NOFOLLOW`. None of that crosses a container boundary, so this is a sibling
rather than a subclass — the shape is the same, the mechanism is not, and
pretending one is the other by inheritance is how a weaker guarantee ends up
wearing a stronger one's name.

What is identical, and is the part that matters: **every effect goes through the
Gate, and a denial comes back as an observation.** The model on this plane sees
exactly what it sees on the other one.

What is different, stated once:

* File reads and writes travel as shell. There is no remote `open()`, so a write
  is `base64 -d` into the path and a read is `cat`. Content is base64 on the wire
  in both directions, which is the only encoding that survives arbitrary bytes
  through a text-mode exec channel intact — the same class of bug as
  `O_BINARY` on Windows (`STATUS.md`, finding M1-1), and worth spending the 33%
  size overhead to avoid.
* The effect is one `exec` round trip, so the TOCTOU window is the round trip.
  `gate/remote.py` says why that is acceptable *here* and nowhere else.
"""

from __future__ import annotations

import base64
import shlex
import time
from dataclasses import dataclass
from typing import Any

from ..gate.gate import Gate
from ..gate.remote import RemoteArgvTarget, RemoteCapability, RemoteFsTarget
from ..gate.types import CapabilityRequest, Reversibility, Verb
from ..ledger.events import Meter, TrustLabel
from ..venues.base import VenueRequest, VenueResult, VenueUnavailable, choose
from ..venues.remote import RemoteVenue, TransportFailed
from .std import MAX_READ_CHARS, _denied, _ms

#: Written files are read back through the same channel, so a size ceiling here
#: is a ceiling on one exec argument. Well below any shell's ARG_MAX.
MAX_WRITE_BYTES = 512_000


@dataclass
class RemoteTools:
    """Read / write / list / bash, in a remote workspace, all through the Gate."""

    gate: Gate
    venue: RemoteVenue
    workspace: str
    actor: str = "agent"
    #: The model wrote the arguments. Saying otherwise is how the untrusted gate
    #: stops meaning anything.
    trust: TrustLabel = TrustLabel.UNTRUSTED_MODEL_OUTPUT
    default_timeout_s: float = 120.0

    def _submit(self, verb: Verb, tool: str, spec: Any, rev: Reversibility, intent: str = ""):
        return self.gate.submit(CapabilityRequest(
            actor=self.actor, verb=verb, trust=self.trust, reversibility=rev,
            tool=tool, target_spec=spec, intent=intent, venue=self.venue.name,
        ))

    def _exec(self, target: RemoteArgvTarget, *, timeout_s: float) -> VenueResult:
        request = VenueRequest(
            timeout_s=timeout_s, min_isolation=self.venue.isolation(), allow_network=True
        )
        venue = choose([self.venue], request)
        return venue.run(RemoteCapability(target), request)

    def _shell(self, script: str, *, timeout_s: float, cwd: str | None = None) -> VenueResult:
        """Internal shell, built here rather than by the Gate's resolver.

        Used only for the transport of an already-authorised file effect — the
        path in it came out of a `RemoteFsTarget`, not out of the model. A tool
        the model can reach never calls this; it calls `bash`, which resolves.
        """
        return self._exec(
            RemoteArgvTarget(
                kind="remote_argv",
                argv=("bash", "-lc", script),
                cwd=cwd or self.workspace,
                script=script,
                venue=self.venue.name,
            ),
            timeout_s=timeout_s,
        )

    # -- files ----------------------------------------------------------------

    def read_file(self, path: str, *, offset: int = 0, limit: int = 400) -> dict[str, Any]:
        out = self._submit(Verb.READ, "read_file", path, Reversibility.OVERLAY)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        target = out.handle.consume()
        assert isinstance(target, RemoteFsTarget)
        try:
            # No `-w0`: BusyBox's base64 has no such flag and many benchmark
            # images are Alpine. Wrapped output is fine — `b64decode` ignores
            # newlines — and assuming GNU coreutils inside someone else's
            # container is exactly the kind of guess that fails on task 40.
            r = self._shell(
                f"base64 -- {shlex.quote(target.path)}", timeout_s=30.0
            )
            if not r.ok:
                result, ok = {"error": (r.stderr or r.stdout or "read failed").strip()}, False
            else:
                text = base64.b64decode(r.stdout.strip() or b"").decode(
                    "utf-8", errors="replace"
                )
                lines = text.splitlines()
                window = lines[offset: offset + limit]
                result = {
                    "path": self._rel(target.path),
                    "total_lines": len(lines),
                    "offset": offset,
                    "returned": len(window),
                    "content": "\n".join(window)[:MAX_READ_CHARS],
                }
                ok = True
        except (TransportFailed, VenueUnavailable, ValueError) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "read_file", "path": target.path})
        return result

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        # SNAPSHOT, not COMPENSATION: the compensator captures inverses by
        # reading the prior state on *this* filesystem, and it cannot read that
        # one. Declaring COMPENSATION here would put an inverse on the record
        # that nothing can apply, which `Compensator.capture` already refuses to
        # do. What actually reverses this plane is discarding the container.
        out = self._submit(Verb.WRITE, "write_file", path, Reversibility.SNAPSHOT)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        target = out.handle.consume()
        assert isinstance(target, RemoteFsTarget)
        payload = content.encode("utf-8")
        if len(payload) > MAX_WRITE_BYTES:
            self.gate.settle(out.handle, ok=False, meter=Meter(wall_ms=_ms(started)),
                             detail={"tool": "write_file", "reason": "too large"})
            return {"error": f"content exceeds {MAX_WRITE_BYTES} bytes; write it in pieces"}
        blob = base64.b64encode(payload).decode("ascii")
        quoted = shlex.quote(target.path)
        script = (
            f"mkdir -p -- \"$(dirname {quoted})\" && "
            f"printf %s {shlex.quote(blob)} | base64 -d > {quoted}"
        )
        try:
            r = self._shell(script, timeout_s=60.0)
            if r.ok:
                result, ok = {"written": True, "bytes": len(payload),
                              "path": self._rel(target.path), "undo": "snapshot"}, True
            else:
                result, ok = {"error": (r.stderr or r.stdout or "write failed").strip()}, False
        except (TransportFailed, VenueUnavailable) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "write_file", "path": target.path,
                                 "bytes": len(payload)})
        return result

    def list_dir(self, path: str = ".", *, limit: int = 500) -> dict[str, Any]:
        out = self._submit(Verb.READ, "list_dir", path, Reversibility.OVERLAY)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        target = out.handle.consume()
        assert isinstance(target, RemoteFsTarget)
        try:
            # `pipefail`, because without it the pipeline reports `head`'s exit
            # code and a listing of a directory that does not exist comes back
            # as a successful listing of nothing.
            r = self._shell(
                f"set -o pipefail; ls -1Ap -- {shlex.quote(target.path)} "
                f"| head -n {int(limit)}",
                timeout_s=30.0,
            )
            entries = [ln for ln in r.stdout.splitlines() if ln]
            result, ok = {"path": self._rel(target.path), "entries": entries,
                          "count": len(entries)}, r.ok
            if not ok:
                result = {"error": (r.stderr or r.stdout or "list failed").strip()}
        except (TransportFailed, VenueUnavailable) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "list_dir", "path": target.path})
        return result

    # -- execution ------------------------------------------------------------

    def bash(self, command: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Run a shell command in the workspace.

        The spec is `{"script": ...}` rather than the bare string the model
        supplied, because `RemoteResolver` refuses a bare string on purpose: the
        request has to *declare* that a shell is involved, and the resolver — not
        the model — builds the argv.
        """
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        out = self._submit(
            Verb.EXECUTE, "bash", {"script": command}, Reversibility.SNAPSHOT,
            intent=command[:200],
        )
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        target = out.handle.consume()
        assert isinstance(target, RemoteArgvTarget)
        try:
            r = self._exec(target, timeout_s=timeout)
            result = {
                "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr,
                "venue": r.venue, "isolation": r.isolation.name,
                "timed_out": r.timed_out, "duration_ms": r.duration_ms,
            }
            ok = r.ok
        except VenueUnavailable as exc:
            result, ok = {"error": str(exc), "denied": True,
                          "rule": "__isolation_unavailable__"}, False
        except TransportFailed as exc:
            # Not an exit code. The command did not happen, and the agent needs
            # to know the difference before it reasons about the output.
            result, ok = {"error": f"the command did not run: {exc}",
                          "transport_failed": True}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "bash", "script": target.script[:2000],
                                 "exit_code": result.get("exit_code")})
        return result

    # -- helpers --------------------------------------------------------------

    def _rel(self, path: str) -> str:
        return path[len(self.workspace):].lstrip("/") or "."


def probe_environment(tools: RemoteTools, *, max_entries: int = 60) -> str:
    """One cheap structured look at the box, for turn zero.

    `research.md` §2.1 attributes a large share of Goose's 20-40x token advantage
    to priming the model with structure instead of letting it discover the
    environment through five exploratory tool calls. This is that, as one round
    trip: what the box is, where we are, and what is in front of us.

    It is deliberately a single `bash` through the Gate — recorded, metered and
    refusable like anything else — rather than a privileged back door for the
    harness.
    """
    script = (
        "echo '## cwd'; pwd; "
        "echo '## os'; (cat /etc/os-release 2>/dev/null | head -n 2) || uname -a; "
        "echo '## tools'; for t in python3 pip node npm go cargo make git gcc jq; do "
        "command -v $t >/dev/null && echo -n \"$t \"; done; echo; "
        f"echo '## tree'; ls -1Ap | head -n {int(max_entries)}"
    )
    r = tools.bash(script, timeout_s=60.0)
    if r.get("denied") or r.get("error"):
        return f"environment probe unavailable: {r.get('error') or r.get('rule')}"
    return (r.get("stdout") or "").strip()[:4000]

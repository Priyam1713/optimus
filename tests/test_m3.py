"""M3: the envelope, the remote plane, the loop, the adapter and the report.

The interesting tests here are the ones that try to *break* the new
authorisation path, because M3 adds the first thing in the project that lets an
agent act without a human in the room. If an envelope can be forged, borrowed
from another workspace, replayed past its ceiling or used to walk through a deny
rule, then everything upstream of it was decoration.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import uuid

import pytest

from optimus.context.episodes import Episode, EpisodeKind
from optimus.context.window import ContextBudget, ContextWindow
from optimus.gate.envelope import ANY_WORKSPACE, Envelope, EnvelopeRefused, issue
from optimus.gate.gate import Gate
from optimus.gate.policy import benchmark_policy
from optimus.gate.remote import RemoteResolver, posix_norm
from optimus.gate.targets import TargetRefused
from optimus.gate.types import CapabilityRequest, Reversibility, Verb, Verdict
from optimus.ledger.chain import Chain, now_ms
from optimus.ledger.events import TrustLabel
from optimus.ledger.keys import AgentKey, OwnerKey
from optimus.loop.agent import AgentLoop, LoopLimits, clamp
from optimus.loop.llm import ModelReply, ScriptedLLM, ToolCall, Usage
from optimus.meter import aggregate
from optimus.report import Report, Trial, pass_at_k, pass_hat_k
from optimus.tools.remote import RemoteTools
from optimus.venues.base import Isolation
from optimus.venues.remote import RemoteExec, RemoteVenue, TransportFailed

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="a POSIX shell is needed")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _chain() -> Chain:
    return Chain(AgentKey.generate())


def _envelope(owner: OwnerKey, **over) -> Envelope:
    kw = dict(
        principal="operator@example",
        actor="agent",
        workspace="/work",
        venues=("harbor",),
        max_actions=10,
        reason="test",
    )
    kw.update(over)
    return issue(owner, **kw)


def _request(**over) -> CapabilityRequest:
    kw = dict(
        actor="agent",
        verb=Verb.WRITE,
        trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT,
        reversibility=Reversibility.SNAPSHOT,
        tool="write_file",
        target_spec="notes.txt",
        venue="harbor",
    )
    kw.update(over)
    return CapabilityRequest(**kw)


def _gate(policy=None, workspace="/work", run_id="run-1") -> Gate:
    return Gate(
        _chain(),
        policy or benchmark_policy(),
        RemoteResolver(workspace, venue="harbor"),
        run_id=run_id,
    )


class RecordingTransport:
    """Returns scripted results and keeps every command it was handed."""

    def __init__(self, results=None, fail=False):
        self.results = list(results or [])
        self.commands: list[tuple[tuple[str, ...], str]] = []
        self.fail = fail

    def __call__(self, argv, *, cwd, timeout_s):
        self.commands.append((tuple(argv), cwd))
        if self.fail:
            raise OSError("the container is gone")
        if self.results:
            return self.results.pop(0)
        return RemoteExec(exit_code=0, stdout="", stderr="")


class ShellTransport:
    """A real shell, so the remote tool plane is exercised end to end.

    `cwd` is applied inside the shell rather than by `subprocess`, because the
    workspace is a POSIX path this host cannot chdir into directly — which is
    exactly the situation the remote plane exists for.
    """

    def __call__(self, argv, *, cwd, timeout_s):
        argv = list(argv)
        script = argv[2] if len(argv) == 3 and argv[1] in ("-lc", "-c") else shlex.join(argv)
        proc = subprocess.run(
            [BASH, "-lc", f"cd {shlex.quote(cwd)} && {script}"],
            capture_output=True, text=True, timeout=timeout_s + 10,
            env={**os.environ},
        )
        return RemoteExec(
            exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )


# ==========================================================================
# the envelope
# ==========================================================================

class TestEnvelope:
    def test_unsigned_envelope_is_refused(self):
        gate = _gate()
        bare = Envelope(
            envelope_id="env_forged", principal="me", actor="agent",
            verbs=("write",), venues=("harbor",), workspace="/work",
            max_actions=10, expires_ms=now_ms() + 10_000,
        )
        with pytest.raises(EnvelopeRefused, match="unsigned"):
            gate.open_envelope(bare, owner_fingerprint="whatever")
        assert gate.envelope is None

    def test_verifying_without_a_fingerprint_is_refused(self):
        """The exact mistake `chain.verify` exists to prevent, repeated here."""
        owner = OwnerKey.generate()
        gate = _gate()
        with pytest.raises(EnvelopeRefused, match="fingerprint is required"):
            gate.open_envelope(_envelope(owner), owner_fingerprint="")

    def test_a_different_owner_is_refused(self):
        gate = _gate()
        stranger = OwnerKey.generate()
        with pytest.raises(EnvelopeRefused, match="signed by"):
            gate.open_envelope(
                _envelope(stranger), owner_fingerprint=OwnerKey.generate().fingerprint
            )

    def test_tampering_with_the_body_breaks_the_signature(self):
        owner = OwnerKey.generate()
        good = _envelope(owner, max_actions=1)
        forged = Envelope.from_dict({**good.to_dict(), "max_actions": 100_000})
        gate = _gate()
        with pytest.raises(EnvelopeRefused, match="does not verify"):
            gate.open_envelope(forged, owner_fingerprint=owner.fingerprint)

    def test_a_refusal_is_on_the_record(self):
        gate = _gate()
        with pytest.raises(EnvelopeRefused):
            gate.open_envelope(
                _envelope(OwnerKey.generate()), owner_fingerprint="nope"
            )
        assert any(e.kind == "envelope.refused" for e in gate.chain.events)

    def test_without_an_envelope_every_mutation_parks(self):
        gate = _gate()
        out = gate.submit(_request())
        assert out.verdict is Verdict.NEEDS_APPROVAL
        assert out.rule_id == "__untrusted_cannot_mutate__"

    def test_a_valid_envelope_clears_the_untrusted_invariant(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(_envelope(owner), owner_fingerprint=owner.fingerprint)
        out = gate.submit(_request())
        assert out.allowed and out.handle is not None
        assert gate.envelope_uses == 1
        assert any(e.kind == "envelope.used" for e in gate.chain.events)

    def test_an_envelope_does_not_clear_a_deny_rule(self):
        """The whole point: it stands in for the human, not for the rules."""
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(_envelope(owner), owner_fingerprint=owner.fingerprint)
        out = gate.submit(_request(target_spec=".env"))
        assert out.verdict is Verdict.DENY
        assert out.rule_id == "deny-sensitive-write"

    def test_an_envelope_does_not_reach_another_verb(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, verbs=("read",)), owner_fingerprint=owner.fingerprint
        )
        out = gate.submit(_request())
        assert out.verdict is Verdict.NEEDS_APPROVAL
        assert "does not cover verb" in out.reason

    def test_an_envelope_does_not_reach_another_venue(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, venues=("laptop",)), owner_fingerprint=owner.fingerprint
        )
        assert gate.submit(_request()).verdict is Verdict.NEEDS_APPROVAL

    def test_an_envelope_does_not_reach_another_workspace(self):
        owner = OwnerKey.generate()
        gate = _gate(workspace="/elsewhere")
        gate.open_envelope(_envelope(owner), owner_fingerprint=owner.fingerprint)
        out = gate.submit(_request())
        assert out.verdict is Verdict.NEEDS_APPROVAL
        assert "outside the envelope's workspace" in out.reason

    def test_venue_scope_covers_a_workspace_not_known_in_advance(self):
        """None of Terminal-Bench 2.0's 89 tasks declares a `workdir`, so each
        one's workspace comes from its own image and cannot be named when the
        envelope is signed."""
        owner = OwnerKey.generate()
        gate = _gate(workspace="/whatever-the-image-said")
        gate.open_envelope(
            _envelope(owner, workspace=ANY_WORKSPACE),
            owner_fingerprint=owner.fingerprint,
        )
        assert gate.submit(_request()).allowed
        assert gate.envelope.venue_scoped is True

    def test_venue_scope_still_does_not_reach_another_venue(self):
        """The widening is on the path clause only. The venue clause is what
        actually confines a venue-scoped envelope, so it had better hold."""
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, workspace=ANY_WORKSPACE, venues=("somewhere-else",)),
            owner_fingerprint=owner.fingerprint,
        )
        assert gate.submit(_request()).verdict is Verdict.NEEDS_APPROVAL

    def test_venue_scope_still_obeys_verbs_ceiling_and_deny_rules(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, workspace=ANY_WORKSPACE, verbs=("write",), max_actions=1),
            owner_fingerprint=owner.fingerprint,
        )
        # Deny still denies.
        assert gate.submit(_request(target_spec=".env")).rule_id == "deny-sensitive-write"
        # A verb outside the set still parks.
        assert gate.submit(
            _request(verb=Verb.EXECUTE, tool="bash", target_spec={"script": "ls"})
        ).verdict is Verdict.NEEDS_APPROVAL
        # And the ceiling still bites.
        assert gate.submit(_request(target_spec="a.txt")).allowed
        assert gate.submit(_request(target_spec="b.txt")).verdict is Verdict.NEEDS_APPROVAL

    def test_venue_scope_does_not_defeat_the_resolver(self):
        """The primary containment is the resolver's, and it is untouched: a
        venue-scoped envelope is not a licence to leave the workspace."""
        owner = OwnerKey.generate()
        gate = _gate(workspace="/work")
        gate.open_envelope(
            _envelope(owner, workspace=ANY_WORKSPACE),
            owner_fingerprint=owner.fingerprint,
        )
        out = gate.submit(_request(target_spec="../../etc/passwd"))
        assert out.verdict is Verdict.DENY
        assert out.rule_id == "__target_refused__"

    def test_the_widening_is_written_into_the_signed_document(self):
        """An auditor reading the ledger must see the scope, not infer it from a
        blank field."""
        owner = OwnerKey.generate()
        envelope = _envelope(owner, workspace=ANY_WORKSPACE)
        assert envelope.to_dict()["workspace"] == "*"
        assert "any path in harbor" in envelope.describe()
        gate = _gate()
        gate.open_envelope(envelope, owner_fingerprint=owner.fingerprint)
        opened = [e for e in gate.chain.events if e.kind == "envelope.opened"][-1]
        assert opened.payload["workspace"] == "*"
        # And it is still covered by the signature, so it cannot be edited in.
        forged = Envelope.from_dict({**_envelope(owner, workspace="/work").to_dict(),
                                     "workspace": ANY_WORKSPACE})
        with pytest.raises(EnvelopeRefused, match="does not verify"):
            _gate().open_envelope(forged, owner_fingerprint=owner.fingerprint)

    def test_a_denied_action_does_not_spend_the_ceiling(self):
        """`max_actions` counts actions, and a denied action did not happen.

        Spending on denial would also let an agent burn the operator's entire
        budget on work the policy was always going to refuse.
        """
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, max_actions=1), owner_fingerprint=owner.fingerprint
        )
        assert gate.submit(_request(target_spec=".env")).verdict is Verdict.DENY
        assert gate.envelope_uses == 0
        assert gate.submit(_request(target_spec="ok.txt")).allowed
        assert gate.envelope_uses == 1

    def test_a_parked_action_does_not_spend_the_ceiling(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, verbs=("read", "write", "execute"), max_actions=5),
            owner_fingerprint=owner.fingerprint,
        )
        # `delete` is an approval rule in `benchmark_policy`, and there is nobody
        # to approve it, so it parks — and costs nothing.
        gate.submit(_request(verb=Verb.DELETE, tool="delete_file"))
        assert gate.envelope_uses == 0

    def test_the_action_ceiling_is_enforced(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, max_actions=2), owner_fingerprint=owner.fingerprint
        )
        assert gate.submit(_request(target_spec="a.txt")).allowed
        assert gate.submit(_request(target_spec="b.txt")).allowed
        third = gate.submit(_request(target_spec="c.txt"))
        assert third.verdict is Verdict.NEEDS_APPROVAL
        assert "ceiling" in third.reason
        assert any(e.kind == "envelope.exhausted" for e in gate.chain.events)

    def test_an_envelope_that_expires_mid_run_stops_acting(self):
        """Admission is not the only check: a long run can outlive its grant."""
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(
            _envelope(owner, ttl_ms=60_000), owner_fingerprint=owner.fingerprint
        )
        assert gate.submit(_request()).allowed
        # Time passes.
        expired = Envelope.from_dict(
            {**gate.envelope.to_dict(), "expires_ms": now_ms() - 1}
        )
        gate._envelope = expired
        out = gate.submit(_request(target_spec="later.txt"))
        assert out.verdict is Verdict.NEEDS_APPROVAL
        assert "expired" in out.reason

    def test_an_expired_envelope_is_refused_at_the_door(self):
        """Not only per action.

        A real trial opened a two-day-expired envelope, logged "envelope
        opened", then correctly refused all 31 of its actions one at a time
        across nine minutes of local inference. Everything downstream was right;
        the door was what said yes.
        """
        owner = OwnerKey.generate()
        gate = _gate()
        with pytest.raises(EnvelopeRefused, match="expired"):
            gate.open_envelope(
                _envelope(owner, ttl_ms=-60_000), owner_fingerprint=owner.fingerprint
            )
        assert gate.envelope is None
        assert any(e.kind == "envelope.refused" for e in gate.chain.events)

    def test_freezing_closes_the_envelope(self):
        owner = OwnerKey.generate()
        gate = _gate()
        gate.open_envelope(_envelope(owner), owner_fingerprint=owner.fingerprint)
        gate.freeze("operator pulled the cord")
        assert gate.envelope is None

    def test_the_gate_cannot_mint_one(self):
        """There is no code path from a Gate to an OwnerKey. Assert the shape."""
        gate = _gate()
        assert not any(
            isinstance(getattr(gate, name, None), OwnerKey) for name in dir(gate)
        )
        assert not hasattr(gate, "issue_envelope")


# ==========================================================================
# the remote plane
# ==========================================================================

class TestRemoteResolution:
    def test_traversal_is_refused_without_touching_anything(self):
        r = RemoteResolver("/work", venue="harbor")
        with pytest.raises(TargetRefused, match="escapes"):
            r.resolve_path("../../etc/passwd")

    def test_an_absolute_path_outside_the_workspace_is_refused(self):
        r = RemoteResolver("/work", venue="harbor")
        with pytest.raises(TargetRefused, match="escapes"):
            r.resolve_path("/etc/shadow")

    def test_a_sibling_directory_is_not_inside(self):
        """Containment by components, not by string prefix."""
        r = RemoteResolver("/work", venue="harbor")
        with pytest.raises(TargetRefused):
            r.resolve_path("/work-secrets/key")

    def test_a_bare_shell_string_is_refused(self):
        r = RemoteResolver("/work", venue="harbor")
        with pytest.raises(TargetRefused, match="ambiguous"):
            r.resolve_exec("rm -rf /")

    def test_a_declared_script_resolves_and_is_recorded_verbatim(self):
        r = RemoteResolver("/work", venue="harbor")
        target = r.resolve_exec({"script": "echo 'hi there'"})
        assert target.argv == ("bash", "-lc", "echo 'hi there'")
        assert target.script == "echo 'hi there'"
        assert target.attrs()["script"] == "echo 'hi there'"

    def test_the_remote_plane_never_claims_to_pin_identity(self):
        r = RemoteResolver("/work", venue="harbor")
        assert r.resolve_path("a.txt").attrs()["pins_identity"] is False
        assert r.resolve_exec(["ls"]).attrs()["pins_identity"] is False

    def test_posix_norm_collapses_without_io(self):
        assert posix_norm("/work/a/../b") == "/work/b"

    def test_attribute_names_match_the_local_plane(self):
        """One policy document has to govern both planes or they will drift."""
        from optimus.gate.targets import resolve_fs

        remote = RemoteResolver("/work", venue="harbor").resolve_path("src/x.py").attrs()
        local = resolve_fs(".", os.getcwd()).attrs()
        shared = {"kind", "path", "relpath", "name", "suffix", "exists", "pins_identity"}
        assert shared <= set(remote) and shared <= set(local)

    def test_a_policy_rule_about_scripts_does_not_break_the_local_plane(self):
        """A missing attribute raises, and a raise denies everything."""
        from optimus.gate.resolvers import WorkspaceResolver

        gate = Gate(_chain(), benchmark_policy(), WorkspaceResolver(os.getcwd()))
        out = gate.submit(_request(verb=Verb.READ, tool="read_file",
                                   target_spec="pyproject.toml", venue="local"))
        assert out.allowed, out.reason


class TestGraderProtection:
    def test_a_write_into_the_grader_is_denied(self):
        owner = OwnerKey.generate()
        gate = Gate(
            _chain(), benchmark_policy(), RemoteResolver("/", venue="harbor"),
            run_id="r",
        )
        gate.open_envelope(
            _envelope(owner, workspace="/"), owner_fingerprint=owner.fingerprint
        )
        out = gate.submit(_request(target_spec="/tests/test_outputs.py"))
        assert out.verdict is Verdict.DENY
        assert out.rule_id == "deny-grader-write"

    def test_a_script_naming_the_grader_trips_the_wire(self):
        owner = OwnerKey.generate()
        gate = Gate(
            _chain(), benchmark_policy(), RemoteResolver("/", venue="harbor"),
            run_id="r",
        )
        gate.open_envelope(
            _envelope(owner, workspace="/"), owner_fingerprint=owner.fingerprint
        )
        out = gate.submit(_request(
            verb=Verb.EXECUTE, tool="bash",
            target_spec={"script": "cat /tests/test_main.py"},
        ))
        assert out.verdict is Verdict.DENY
        assert out.rule_id == "deny-grader-script"


class TestRemoteVenue:
    def test_a_transport_failure_is_not_an_exit_code(self):
        venue = RemoteVenue(RecordingTransport(fail=True), name="harbor")
        tools = RemoteTools(gate=_gate(), venue=venue, workspace="/work")
        owner = OwnerKey.generate()
        tools.gate.open_envelope(_envelope(owner), owner_fingerprint=owner.fingerprint)
        result = tools.bash("true")
        assert result["transport_failed"] is True
        assert "did not run" in result["error"]

    def test_isolation_is_declared_and_refused_honestly(self):
        venue = RemoteVenue(
            RecordingTransport(), name="ssh-box", isolation=Isolation.NONE
        )
        assert venue.isolation() is Isolation.NONE

    def test_the_transport_is_raised_not_swallowed(self):
        venue = RemoteVenue(RecordingTransport(fail=True), name="harbor")
        from optimus.gate.remote import RemoteArgvTarget, RemoteCapability
        from optimus.venues.base import VenueRequest

        cap = RemoteCapability(
            RemoteArgvTarget(kind="remote_argv", argv=("ls",), cwd="/work")
        )
        with pytest.raises(TransportFailed):
            venue.run(cap, VenueRequest(min_isolation=Isolation.CONTAINER))


class TestRemoteTools:
    def _tools(self, transport, workspace="/work"):
        owner = OwnerKey.generate()
        gate = _gate(workspace=workspace)
        gate.open_envelope(
            _envelope(owner, workspace=workspace),
            owner_fingerprint=owner.fingerprint,
        )
        venue = RemoteVenue(transport, name="harbor")
        return RemoteTools(gate=gate, venue=venue, workspace=workspace)

    def test_a_denial_comes_back_as_an_observation(self):
        tools = self._tools(RecordingTransport())
        result = tools.write_file("../escape.txt", "x")
        assert result["denied"] is True
        assert result["rule"] == "__target_refused__"
        assert "error" in result

    def test_write_never_interpolates_the_content_into_the_shell(self):
        """Content is base64 on the wire, so quoting cannot be the bug."""
        transport = RecordingTransport()
        tools = self._tools(transport)
        payload = "'; rm -rf / #\n$(whoami)\n"
        tools.write_file("notes.txt", payload)
        script = transport.commands[-1][0][2]
        assert "rm -rf" not in script
        assert "whoami" not in script
        assert "base64 -d" in script

    def test_bash_reaches_the_transport_with_the_script_intact(self):
        transport = RecordingTransport(
            [RemoteExec(exit_code=0, stdout="hello\n")]
        )
        tools = self._tools(transport)
        result = tools.bash("echo hello")
        assert result["exit_code"] == 0 and result["stdout"] == "hello\n"
        assert transport.commands[-1][0] == ("bash", "-lc", "echo hello")

    def test_the_ledger_records_the_command_verbatim(self):
        transport = RecordingTransport()
        tools = self._tools(transport)
        tools.bash("make test")
        settled = [e for e in tools.gate.chain.events if e.kind == "effect.settled"]
        assert settled[-1].payload["detail"]["script"] == "make test"

    def test_an_oversized_write_is_refused_before_the_wire(self):
        transport = RecordingTransport()
        tools = self._tools(transport)
        before = len(transport.commands)
        result = tools.write_file("big.bin", "x" * 600_000)
        assert "exceeds" in result["error"]
        assert len(transport.commands) == before

    @needs_bash
    def test_a_real_round_trip_through_a_real_shell(self):
        workspace = f"/tmp/optimus-m3-{uuid.uuid4().hex[:8]}"
        subprocess.run([BASH, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)
        try:
            tools = self._tools(ShellTransport(), workspace=workspace)
            # Long enough that `base64` wraps its output, and awkward enough
            # that a quoting bug would show. `-w0` is not portable to BusyBox,
            # so the decoder has to tolerate the wrapping instead.
            payload = (
                "line one\nline two\nquote ' and $VAR and \\ backslash\n"
                + "x" * 300 + "\n" + "unicode: café — naïve\n"
            )
            written = tools.write_file("nested/dir/file.txt", payload)
            assert written["written"] is True

            read_back = tools.read_file("nested/dir/file.txt")
            assert read_back["content"] == payload.rstrip("\n")
            assert read_back["total_lines"] == 5

            listing = tools.list_dir(".")
            assert "nested/" in listing["entries"]

            ran = tools.bash("cat nested/dir/file.txt | wc -l")
            assert ran["exit_code"] == 0 and ran["stdout"].strip() == "5"
        finally:
            subprocess.run(
                [BASH, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )

    @needs_bash
    def test_listing_a_directory_that_is_not_there_is_a_failure(self):
        """`ls bad | head` exits 0 without `pipefail`, and reports nothing found
        as a successful listing of nothing."""
        workspace = f"/tmp/optimus-m3-{uuid.uuid4().hex[:8]}"
        subprocess.run([BASH, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)
        try:
            tools = self._tools(ShellTransport(), workspace=workspace)
            result = tools.list_dir("no-such-directory")
            assert "error" in result and "entries" not in result
        finally:
            subprocess.run(
                [BASH, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )


# ==========================================================================
# the loop
# ==========================================================================

def _reply(*calls, text="", usage=None, error="") -> ModelReply:
    return ModelReply(
        text=text,
        tool_calls=tuple(
            ToolCall(call_id=f"c{i}", name=n, arguments=a)
            for i, (n, a) in enumerate(calls, 1)
        ),
        usage=usage or Usage(input_tokens=100, output_tokens=20, cost_usd=0.001),
        error=error,
    )


def _loop(replies, *, limits=None, transport=None, budget=None):
    owner = OwnerKey.generate()
    gate = _gate()
    gate.open_envelope(_envelope(owner, max_actions=500),
                       owner_fingerprint=owner.fingerprint)
    venue = RemoteVenue(transport or RecordingTransport(), name="harbor")
    tools = RemoteTools(gate=gate, venue=venue, workspace="/work")
    window = ContextWindow(budget or ContextBudget(total=32_000, keep_recent=4))
    return AgentLoop(
        gate=gate, tools=tools, window=window,
        llm=ScriptedLLM(list(replies)), limits=limits or LoopLimits(), run_id="run-1",
    )


class TestLoop:
    def test_finish_stops_the_run_cleanly(self):
        loop = _loop([
            _reply(("bash", {"command": "ls"})),
            _reply(("finish", {"summary": "did the thing"})),
        ])
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"
        assert out.summary == "did the thing"
        assert out.turns == 2

    def test_a_no_action_turn_is_counted_corrected_and_then_broken(self):
        """The LangChain doom-loop finding, as a mechanism rather than a note."""
        loop = _loop([_reply(text="thinking...")] * 5,
                     limits=LoopLimits(max_no_action_streak=3))
        out = loop.run("do the thing")
        assert out.stop_reason == "stalled"
        assert out.no_action_turns == 3
        breakers = [
            e for e in loop.gate.chain.events
            if e.kind == "loop.breaker" and e.payload["kind"] == "no_action"
        ]
        assert len(breakers) == 3
        # The correction actually reached the model, not just the log.
        assert any(
            "called no tool" in str(m.get("content"))
            for m in loop.messages() if m["role"] == "user"
        )

    def test_one_idle_turn_is_survivable(self):
        loop = _loop([
            _reply(text="hmm"),
            _reply(("finish", {"summary": "recovered"})),
        ])
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"
        assert out.no_action_turns == 1

    def test_repeating_an_identical_action_is_named_and_then_stopped(self):
        loop = _loop(
            [_reply(("bash", {"command": "ls"}))] * 12,
            limits=LoopLimits(max_repeat_action=3, max_turns=12),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "looping"
        assert out.repeated_actions >= 1

    def test_the_loop_stops_when_asked(self):
        """`asyncio.to_thread` cannot be cancelled, so the loop has to agree to
        stop. A real trial kept a GPU busy long after Harbor had timed out and
        moved on."""
        loop = _loop([_reply(("bash", {"command": f"echo {i}"})) for i in range(10)],
                     limits=LoopLimits(max_turns=10))
        loop.request_stop()
        out = loop.run("do the thing")
        assert out.stop_reason == "cancelled"
        assert out.turns == 1
        # It still wrote its own ending, so a receipt exists.
        assert any(e.kind == "run.finished" for e in loop.gate.chain.events)

    def test_a_wall_of_refusals_stops_the_run(self):
        """A run where nothing is permitted cannot succeed, and every further
        turn is pure cost. A real trial spent 30 turns and 232K tokens being
        correctly refused by an expired envelope."""
        loop = _loop(
            [_reply(("write_file", {"path": f"../escape{i}.txt", "content": "x"}))
             for i in range(12)],
            limits=LoopLimits(max_consecutive_denials=4, max_turns=12),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "blocked"
        assert out.gate_denials == 4
        assert out.turns == 4
        assert any(
            e.payload.get("kind") == "blocked"
            for e in loop.gate.chain.events if e.kind == "loop.breaker"
        )

    def test_one_refusal_among_successes_does_not_stop_the_run(self):
        loop = _loop([
            _reply(("bash", {"command": "ls"})),
            _reply(("write_file", {"path": "../nope.txt", "content": "x"})),
            _reply(("bash", {"command": "pwd"})),
            _reply(("finish", {"summary": "carried on"})),
        ], limits=LoopLimits(max_consecutive_denials=2))
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"
        assert out.gate_denials == 1

    def test_a_fatal_provider_error_stops_at_once(self):
        """A 404 for a model that does not exist answers the same every time.

        A real run spent ten minutes and three attempts learning that.
        """
        loop = _loop(
            [ModelReply(error="NotFoundError: no such model", retryable=False,
                        usage=Usage())] * 5,
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "provider_error"
        assert out.provider_errors == 1
        assert out.turns == 1

    def test_a_transient_provider_error_is_waited_out(self):
        """Eleven good turns then a rate limit should not lose the run."""
        loop = _loop(
            [ModelReply(error="RateLimitError: 429", retryable=True, usage=Usage()),
             ModelReply(error="RateLimitError: 429", retryable=True, usage=Usage()),
             _reply(("finish", {"summary": "recovered"}))],
            limits=LoopLimits(retry_backoff_s=0.01, max_backoff_s=0.02),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"
        assert out.provider_errors == 2
        assert out.backoff_ms > 0

    def test_transient_errors_eventually_give_up_too(self):
        loop = _loop(
            [ModelReply(error="RateLimitError: 429", retryable=True, usage=Usage())] * 10,
            limits=LoopLimits(max_transient_errors=3, retry_backoff_s=0.01,
                              max_backoff_s=0.02),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "provider_unavailable"
        assert out.provider_errors == 3

    def test_the_providers_own_retry_delay_is_honoured(self):
        """A real 429 carried "Please retry in 59.28s". Guessing when the
        provider has told you is how a run either hammers the limit or sleeps
        far longer than it needed to."""
        from optimus.loop.llm import retry_after_seconds

        class Boom(Exception):
            pass

        assert retry_after_seconds(Boom("429 ... Please retry in 59.28s.")) == 59.28
        assert retry_after_seconds(Boom('{"retryDelay": "30s"}')) == 30.0
        assert retry_after_seconds(Boom("no hint here")) == 0.0

    def test_the_retry_delay_raises_the_floor_but_not_past_the_cap(self):
        loop = _loop(
            [ModelReply(error="429", retryable=True, retry_after_s=5.0, usage=Usage()),
             _reply(("finish", {"summary": "ok"}))],
            # Our own curve would wait 0.01s; the provider asked for 5, and the
            # cap says never more than 0.02.
            limits=LoopLimits(retry_backoff_s=0.01, max_backoff_s=0.02),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"
        assert out.backoff_ms <= 30

    def test_a_failed_call_is_not_an_idle_turn(self):
        """The bug a real Terminal-Bench run exposed: three rate-limited calls
        were published as three no-action turns, when the model had idled zero
        times. That corrupts the one metric this project exists to publish."""
        loop = _loop(
            [ModelReply(error="RateLimitError: 429", retryable=True, usage=Usage()),
             _reply(("finish", {"summary": "ok"}))],
            limits=LoopLimits(retry_backoff_s=0.01, max_backoff_s=0.02),
        )
        out = loop.run("do the thing")
        assert out.provider_errors == 1
        assert out.no_action_turns == 0
        meter = aggregate(loop.gate.chain.events, run_id="run-1")
        assert meter.no_action_turns == 0
        assert meter.provider_errors == 1

    def test_a_rate_limit_is_not_pushed_into_the_context(self):
        """It is the harness's problem. Telling the model costs tokens and
        teaches it nothing."""
        loop = _loop(
            [ModelReply(error="RateLimitError: 429", retryable=True, usage=Usage()),
             _reply(("finish", {"summary": "ok"}))],
            limits=LoopLimits(retry_backoff_s=0.01, max_backoff_s=0.02),
        )
        loop.run("do the thing")
        assert not any(
            "429" in e.content for e in loop.window.episodes
        )

    def test_max_turns_is_a_stop_reason_not_a_crash(self):
        loop = _loop(
            [_reply(("bash", {"command": f"echo {i}"})) for i in range(10)],
            limits=LoopLimits(max_turns=3),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "max_turns" and out.turns == 3

    def test_a_cost_ceiling_stops_the_run(self):
        loop = _loop(
            [_reply(("bash", {"command": "ls -a"}),
                    usage=Usage(input_tokens=10, output_tokens=1, cost_usd=0.5))
             for _ in range(10)],
            limits=LoopLimits(max_cost_usd=0.9, max_turns=10),
        )
        out = loop.run("do the thing")
        assert out.stop_reason == "cost_ceiling"

    def test_the_loop_never_claims_to_have_solved_anything(self):
        loop = _loop([_reply(("finish", {"summary": "done"}))])
        loop.run("do the thing")
        finished = [e for e in loop.gate.chain.events if e.kind == "run.finished"][-1]
        assert finished.payload["solved"] is None

    def test_every_model_call_is_metered_into_the_ledger(self):
        loop = _loop([
            _reply(("bash", {"command": "ls"}),
                   usage=Usage(input_tokens=1_000, output_tokens=50,
                               cached_tokens=800, cost_usd=0.02)),
            _reply(("finish", {"summary": "ok"}),
                   usage=Usage(input_tokens=1_200, output_tokens=30, cost_usd=0.01)),
        ])
        out = loop.run("do the thing")
        meter = aggregate(loop.gate.chain.events, run_id="run-1")
        assert meter.turns == 2
        assert meter.input_tokens == 2_200 and meter.output_tokens == 80
        assert meter.cached_tokens == 800
        assert round(meter.cost_usd, 4) == 0.03
        assert meter.total_tokens == out.usage.total_tokens

    def test_tokens_per_solved_task_needs_the_verifier(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("do the thing")
        events = loop.gate.chain.events
        assert aggregate(events, run_id="run-1").tokens_per_solved_task == float("inf")
        assert aggregate(events, run_id="run-1", solved=True).tokens_per_solved_task > 0

    def test_the_contract_and_invariants_are_never_evictable(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("solve the task")
        kinds = {e.kind for e in loop.window.episodes}
        assert EpisodeKind.CONTRACT in kinds and EpisodeKind.INVARIANT in kinds
        for ep in loop.window.episodes:
            if ep.kind in (EpisodeKind.CONTRACT, EpisodeKind.INVARIANT):
                assert ep.permanent

    def test_a_denial_reaches_the_model_as_an_error_episode(self):
        loop = _loop([
            _reply(("write_file", {"path": "../out.txt", "content": "x"})),
            _reply(("finish", {"summary": "gave up on that"})),
        ])
        out = loop.run("write outside")
        assert out.gate_denials == 1
        errors = [e for e in loop.window.episodes if e.kind == EpisodeKind.ERROR]
        assert any("denied" in e.content for e in errors)

    def test_unparseable_tool_arguments_are_an_observation_not_a_crash(self):
        loop = _loop([
            ModelReply(tool_calls=(ToolCall("c1", "bash", {"__unparsed__": "{oops"}),)),
            _reply(("finish", {"summary": "recovered"})),
        ])
        out = loop.run("do the thing")
        assert out.stop_reason == "finished"


class TestPromptBudget:
    """The window counts episodes; the provider bills the rendered request."""

    def test_the_rendered_request_is_larger_than_the_episodes(self):
        loop = _loop([_reply(("bash", {"command": "ls"})),
                      _reply(("finish", {"summary": "ok"}))])
        loop.run("do the thing")
        assert loop.prompt_tokens() > loop.window.used()

    def test_compaction_fires_on_the_rendered_size_not_the_episode_sum(self):
        """A real local run believed it was inside a 24,768-token allowance
        while the server was receiving 28,025, compacted zero times, and was
        refused with "Context size has been exceeded"."""
        big = "x" * 4_000
        loop = _loop(
            [_reply(("bash", {"command": f"echo {i}"})) for i in range(14)],
            # Small enough that the overhead alone is a meaningful share.
            budget=ContextBudget(total=4_000, reserve_output=500, keep_recent=2),
            transport=RecordingTransport(
                [RemoteExec(exit_code=0, stdout=big) for _ in range(14)]
            ),
            limits=LoopLimits(max_turns=14),
        )
        loop.run("do the thing")
        assert loop.compactions_seen() > 0
        # And the thing that was actually over budget came back under it.
        assert loop.prompt_tokens() <= loop.window.budget.fillable

    def test_a_compaction_row_records_the_rendered_numbers(self):
        big = "y" * 4_000
        loop = _loop(
            [_reply(("bash", {"command": f"echo {i}"})) for i in range(10)],
            budget=ContextBudget(total=4_000, reserve_output=500, keep_recent=2),
            transport=RecordingTransport(
                [RemoteExec(exit_code=0, stdout=big) for _ in range(10)]
            ),
            limits=LoopLimits(max_turns=10),
        )
        loop.run("do the thing")
        rows = [e for e in loop.gate.chain.events if e.kind == "context.compacted"]
        assert rows
        payload = rows[0].payload
        assert payload["rendered_before"] > payload["allowance"]
        assert payload["rendered_after"] < payload["rendered_before"]

    def test_the_estimator_calibrates_against_what_the_provider_billed(self):
        """A real run sent 31,921 tokens into a 32,768 window believing it was
        under a 28,672 allowance: the raw estimate was out by about a third,
        because litellm does not know a local model id and the server renders
        its own chat template."""
        loop = _loop([
            _reply(("bash", {"command": "ls"}),
                   usage=Usage(input_tokens=3_000, output_tokens=10)),
            _reply(("finish", {"summary": "ok"}),
                   usage=Usage(input_tokens=3_200, output_tokens=10)),
        ])
        before = loop.prompt_tokens()
        loop.run("do the thing")
        # The provider reported far more than the raw estimate, so the
        # correction went up and stayed up.
        assert loop._calibration > 1.0
        assert loop.prompt_tokens() > loop._raw_prompt_tokens()
        assert before >= 0
        rows = [e for e in loop.gate.chain.events if e.kind == "context.calibrated"]
        assert rows and rows[0].payload["to"] > rows[0].payload["from"]

    def test_the_budget_is_anchored_on_the_last_measured_prompt(self):
        """A ratio learned on a small prompt does not hold at scale.

        A real run learned 1.22 from an 838-token prompt — where a fixed ~185
        token template overhead is 22%, and under 1% of a 25,000-token one — and
        then watched the real prompt climb 28,160 -> 32,424 without compacting.
        """
        loop = _loop([
            _reply(("bash", {"command": "ls"}),
                   usage=Usage(input_tokens=20_000, output_tokens=10)),
            _reply(("finish", {"summary": "ok"}),
                   usage=Usage(input_tokens=20_100, output_tokens=10)),
        ])
        loop.run("do the thing")
        # Whatever the raw estimate says, the floor is what the provider saw.
        assert loop.prompt_tokens() >= 20_000
        assert loop._observed_prompt == 20_100

    def test_the_floor_follows_the_window_in_both_directions(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}),
                             usage=Usage(input_tokens=10_000, output_tokens=5))])
        loop.run("task")
        anchored = loop.prompt_tokens()
        # Growth since the measurement is added...
        for _ in range(8):
            loop.window.push(EpisodeKind.OBSERVATION, "z" * 8_000)
        grown = loop.prompt_tokens()
        assert grown > anchored + 10_000
        # ...and compaction takes it back off, because the delta is signed.
        report = loop.window.compact()
        assert report.evicted > 0
        assert loop.prompt_tokens() < grown

    def test_calibration_rises_at_once_and_falls_slowly(self):
        """Asymmetric on purpose: an under-estimate ends the run, an
        over-estimate only compacts early."""
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop._calibrate(1_000, 3_000)
        assert loop._calibration == pytest.approx(3.0)
        loop._calibrate(1_000, 1_000)          # a much lower observation
        assert loop._calibration > 2.5          # decays, does not snap down
        assert loop._calibration < 3.0

    def test_calibration_never_claims_the_prompt_is_smaller_than_it_looks(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        for _ in range(50):
            loop._calibrate(10_000, 1)
        assert loop._calibration >= 1.0
        assert loop.prompt_tokens() >= loop._raw_prompt_tokens()

    def test_eviction_happens_even_when_the_episodes_look_fine(self):
        """The failure that survived three fixes.

        Detection was right — a real run logged "rendered request is 29,623
        tokens against an allowance of 28,672" on seven consecutive turns — but
        `compact()` sized its eviction from `used()`, which was comfortably
        under `budget.target`, so it evicted nothing and said so every time.
        """
        from optimus.context.window import ContextWindow

        window = ContextWindow(ContextBudget(total=10_000, reserve_output=1_000,
                                             keep_recent=2))
        for i in range(12):
            window.push(EpisodeKind.OBSERVATION, f"observation {i} " * 20)
        settled = window.used()
        # In its own units the window is far under target, so the default
        # compaction is a no-op...
        assert settled < window.budget.target
        assert window.compact().evicted == 0
        # ...and an explicit target still evicts, which is what the loop needs.
        report = window.compact(target=settled // 3)
        assert report.evicted > 0
        assert window.used() < settled

    def test_compaction_leaves_headroom_rather_than_filling_to_the_brim(self):
        big = "q" * 3_000
        loop = _loop(
            [_reply(("bash", {"command": f"echo {i}"})) for i in range(12)],
            budget=ContextBudget(total=6_000, reserve_output=800, keep_recent=2),
            transport=RecordingTransport(
                [RemoteExec(exit_code=0, stdout=big) for _ in range(12)]
            ),
            limits=LoopLimits(max_turns=12),
        )
        loop.run("do the thing")
        rows = [e for e in loop.gate.chain.events if e.kind == "context.compacted"]
        assert rows
        # Under the allowance with room to spare, not scraping it.
        after = rows[0].payload["rendered_after"]
        assert after < rows[0].payload["allowance"]

    def test_an_unshrinkable_window_gives_up_rather_than_spinning(self):
        loop = _loop(
            [_reply(("finish", {"summary": "ok"}))],
            # No room for anything: even the contract alone exceeds this.
            budget=ContextBudget(total=200, reserve_output=100, keep_recent=6),
        )
        loop.run("a task whose contract alone will not fit in this window at all")
        assert any(
            e.payload.get("kind") in ("context_full", "compaction_refused")
            for e in loop.gate.chain.events if e.kind == "loop.breaker"
        )


class TestMessageRendering:
    def test_an_orphaned_tool_result_is_demoted_not_emitted_raw(self):
        """Eviction and the chat wire format disagree about what an atom is."""
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("task")
        loop.window.add(Episode(
            kind=EpisodeKind.OBSERVATION, content="orphan",
            meta={"message": {"role": "tool", "tool_call_id": "gone", "content": "x"}},
        ))
        messages = loop.messages()
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "gone"
            for m in messages
        )
        assert any("compacted call" in str(m.get("content")) for m in messages)

    def test_an_assistant_call_with_no_result_is_demoted_to_text(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("task")
        loop.window.add(Episode(
            kind=EpisodeKind.ACTION, content="orphan call",
            meta={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "nope", "type": "function",
                                "function": {"name": "bash", "arguments": "{}"}}],
            }},
        ))
        for message in loop.messages():
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    assert call["id"] != "nope"

    def test_the_system_block_carries_the_standing_rules(self):
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("task")
        system = loop.messages()[0]
        assert system["role"] == "system"
        assert "Standing rules" in str(system["content"])

    def test_the_standing_rules_are_sent_exactly_once(self):
        """They were going out twice — in the system block and again as user
        turns, every turn, in the part of the prompt caching does not help."""
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("task")
        messages = loop.messages()
        rule = loop.invariants[0]
        occurrences = sum(str(m.get("content", "")).count(rule) for m in messages)
        assert occurrences == 1
        assert not any(
            "[invariant]" in str(m.get("content", "")) for m in messages
        )

    def test_the_rules_are_still_budgeted_and_still_uncompactable(self):
        """Keeping them out of the message list must not take them out of the
        window: the budget has to know what the system block costs, and the
        compaction validator has to be able to refuse to drop them."""
        loop = _loop([_reply(("finish", {"summary": "ok"}))])
        loop.run("task")
        rules = [e for e in loop.window.episodes if e.kind is EpisodeKind.INVARIANT]
        assert len(rules) == len(loop.invariants)
        assert all(e.permanent and e.tokens > 0 for e in rules)


class TestClamp:
    def test_the_middle_is_dropped_and_both_ends_survive(self):
        text = "HEAD" + ("x" * 5_000) + "TAIL"
        out = clamp(text, 200)
        assert out.startswith("HEAD") and out.endswith("TAIL")
        assert "elided from the middle" in out

    def test_short_text_is_untouched(self):
        assert clamp("short", 100) == "short"


# ==========================================================================
# the report
# ==========================================================================

class TestEstimators:
    def test_pass_hat_k_is_not_pass_at_k(self):
        # 10 trials, 5 passes: a lottery ticket is even money, reliability is not.
        assert pass_at_k(10, 5, 2) == pytest.approx(1 - (5 / 10) * (4 / 9))
        assert pass_hat_k(10, 5, 2) == pytest.approx((5 * 4) / (10 * 9))
        assert pass_hat_k(10, 5, 2) < pass_at_k(10, 5, 2)

    def test_perfect_and_hopeless_are_exact(self):
        assert pass_hat_k(4, 4, 4) == 1.0
        assert pass_hat_k(4, 0, 1) == 0.0
        assert pass_at_k(4, 0, 1) == 0.0

    def test_pass_hat_k_falls_as_k_rises(self):
        values = [pass_hat_k(8, 6, k) for k in (1, 2, 4)]
        assert values == sorted(values, reverse=True)

    def test_k_beyond_the_trials_is_refused(self):
        with pytest.raises(ValueError):
            pass_hat_k(3, 2, 4)


class TestReport:
    def _trials(self):
        def make(task, solved, tokens, no_action=0, refused=0, interventions=0):
            return Trial(
                task=task, trial_dir=task, solved=solved, reward=1.0 if solved else 0.0,
                metrics={
                    "total_tokens": tokens, "input_tokens": tokens,
                    "cached_tokens": tokens // 2, "no_action_turns": no_action,
                    "unsafe_attempts_refused": refused,
                    "operator_interventions_required": interventions,
                    "cost_usd": tokens / 100_000,
                },
            )

        return [
            make("alpha", True, 30_000), make("alpha", True, 32_000),
            make("beta", True, 40_000, no_action=2), make("beta", False, 90_000, refused=1),
        ]

    def test_failures_are_charged_to_the_solved_tasks(self):
        report = Report(self._trials())
        assert report.total_tokens == 192_000
        assert report.solved_trials == 3
        assert report.tokens_per_solved_task == pytest.approx(64_000)

    def test_both_reliability_numbers_are_published(self):
        report = Report(self._trials())
        at_k, hat_k = report.pass_at_k(), report.pass_hat_k()
        assert at_k[2] == pytest.approx(1.0)     # alpha 1.0, beta 1.0
        assert hat_k[2] == pytest.approx(0.5)    # alpha 1.0, beta 0.0
        assert hat_k[2] < at_k[2]

    def test_the_gate_metrics_survive_the_join(self):
        report = Report(self._trials())
        assert report.unsafe_attempts_refused == 1
        assert report.no_action_turns_per_task == pytest.approx(0.5)
        assert report.cache_hit_rate == pytest.approx(0.5)

    def test_a_missing_receipt_is_reported_not_hidden(self):
        trials = self._trials()
        trials.append(Trial(task="gamma", trial_dir="g", solved=True, reward=1.0))
        report = Report(trials)
        assert report.without_receipt == 1
        assert "no Optimus receipt" in report.render()

    def test_an_unmetered_run_reports_no_economy_rather_than_zero(self):
        """Harbor's own `oracle` agent writes no receipt. Reporting its cost as
        zero would be the most flattering number available and a fabricated one."""
        report = Report([
            Trial(task="a", trial_dir="a", solved=True, reward=1.0),
            Trial(task="a", trial_dir="b", solved=True, reward=1.0),
        ])
        assert report.metered_trials == 0
        rendered = report.render()
        assert "n/a" in rendered and "tokens/solved-task" not in rendered
        d = report.as_dict()
        assert d["tokens_per_solved_task"] is None
        assert d["cache_hit_rate"] is None
        assert d["total_tokens"] is None
        # The reliability half is still real: it comes from the verifier.
        assert d["pass_hat_k"]["2"] == 1.0

    def test_no_solves_serialises_as_null_rather_than_infinity(self):
        report = Report([
            Trial(task="a", trial_dir="a", solved=False, reward=0.0,
                  metrics={"total_tokens": 10})
        ])
        assert report.as_dict()["tokens_per_solved_task"] is None
        json.dumps(report.as_dict())

    def test_a_multi_reward_task_is_unscored_rather_than_guessed(self, tmp_path):
        from optimus.report import load_trials

        trial = tmp_path / "run" / "task__001"
        (trial / "agent").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({
            "task_name": "task",
            "verifier_result": {"rewards": {"a": 1, "b": 0}},
        }))
        loaded = load_trials(tmp_path)
        assert len(loaded) == 1
        assert loaded[0].solved is False and loaded[0].reward is None
        assert Report(loaded).unscored == 1

    def test_a_run_directory_is_read_end_to_end(self, tmp_path):
        from optimus.report import load_trials

        for i, solved in enumerate([True, False]):
            trial = tmp_path / "run" / f"task__{i}"
            (trial / "agent").mkdir(parents=True)
            (trial / "result.json").write_text(json.dumps({
                "task_name": "task",
                "verifier_result": {"rewards": {"reward": 1 if solved else 0}},
            }))
            (trial / "agent" / "optimus-metrics.json").write_text(json.dumps({
                "total_tokens": 1_000, "input_tokens": 900, "cached_tokens": 400,
                "no_action_turns": 1, "unsafe_attempts_refused": 2,
                "operator_interventions_required": 0, "cost_usd": 0.01,
            }))
        report = Report(load_trials(tmp_path))
        assert len(report.trials) == 2 and report.solved_trials == 1
        assert report.tokens_per_solved_task == pytest.approx(2_000)
        assert report.pass_hat_k()[2] == 0.0
        assert report.pass_at_k()[2] == 1.0


# ==========================================================================
# the adapter
# ==========================================================================

harbor = pytest.importorskip("harbor", reason="harbor is an optional extra")


class TestHarborAdapter:
    def test_it_is_a_registrable_harbor_agent(self):
        from harbor.agents.base import BaseAgent

        from optimus.adapters.harbor import OptimusAgent

        assert issubclass(OptimusAgent, BaseAgent)
        assert OptimusAgent.name() == "optimus"
        assert OptimusAgent.import_path() == "optimus.adapters.harbor:OptimusAgent"
        assert OptimusAgent.SUPPORTS_ATIF is True
        # Nothing abstract left over: harbor instantiates this itself.
        assert not getattr(OptimusAgent, "__abstractmethods__", frozenset())

    def test_the_transport_does_not_double_wrap_the_shell(self):
        from optimus.adapters.harbor import EnvironmentTransport

        assert EnvironmentTransport.to_command(["bash", "-lc", "ls | wc -l"]) == "ls | wc -l"
        assert EnvironmentTransport.to_command(["python", "-c", "x=1"]) == "python -c x=1"

    def test_the_trajectory_validates_against_harbors_own_model(self):
        from harbor.models.trajectories import Trajectory

        from optimus.adapters.harbor import build_trajectory

        loop = _loop([
            _reply(("bash", {"command": "ls"}), text="looking around"),
            _reply(text="idle"),
            _reply(("finish", {"summary": "done"})),
        ])
        outcome = loop.run("do the thing")
        payload = build_trajectory(
            outcome, instruction="do the thing", model_name="test/model",
            session_id="trial__abc", extra={"no_action_turns": outcome.no_action_turns},
        )
        trajectory = Trajectory.model_validate(payload)
        assert trajectory.steps[0].source == "user"
        assert len(trajectory.steps) == len(outcome.steps) + 1
        assert trajectory.final_metrics.extra["no_action_turns"] == 1

    def test_the_written_trajectory_round_trips(self, tmp_path):
        from harbor.models.trajectories import Trajectory

        from optimus.adapters.harbor import build_trajectory, write_trajectory

        loop = _loop([_reply(("finish", {"summary": "done"}))])
        outcome = loop.run("task")
        path = tmp_path / "trajectory.json"
        write_trajectory(path, build_trajectory(
            outcome, instruction="task", model_name="m", session_id="s", extra={},
        ))
        Trajectory.model_validate(json.loads(path.read_text()))

class FakeEnvConfig:
    workdir = None


class FakeEnvironment:
    """The narrow slice of `BaseEnvironment` the adapter actually touches.

    Duck-typed rather than a `BaseEnvironment` subclass on purpose: implementing
    two dozen abstract container methods to test an `exec` bridge would be
    testing Harbor, not us. What this *does* exercise for real is the
    async-to-thread hop, which is the one piece of the adapter that could
    deadlock and which no amount of unit testing around it would catch.
    """

    def __init__(self, bash: str, workspace: str):
        self.task_env_config = FakeEnvConfig()
        self.task_env_config.workdir = workspace
        self.default_user = None
        self._bash = bash
        self.commands: list[str] = []

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        from harbor.environments.base import ExecResult

        self.commands.append(command)
        proc = await asyncio.to_thread(
            subprocess.run,
            [self._bash, "-lc", f"cd {shlex.quote(cwd or '/tmp')} && {command}"],
            capture_output=True, text=True,
        )
        return ExecResult(
            stdout=proc.stdout, stderr=proc.stderr, return_code=proc.returncode
        )


class TestHarborAdapterEndToEnd:
    """The adapter driven the way Harbor drives it: factory, setup, run."""

    @needs_bash
    def test_a_trial_runs_through_harbors_own_factory(self, tmp_path, monkeypatch):
        from harbor.agents.factory import AgentFactory
        from harbor.models.agent.context import AgentContext

        import optimus.adapters.harbor as adapter

        workspace = f"/tmp/optimus-trial-{uuid.uuid4().hex[:8]}"
        subprocess.run([BASH, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)

        owner = OwnerKey.generate()
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(json.dumps(
            issue(owner, principal="test", actor="agent", workspace=workspace,
                  venues=("harbor",), max_actions=50).to_dict()
        ))
        monkeypatch.setenv("OPTIMUS_ENVELOPE", str(envelope_path))
        monkeypatch.setenv("OPTIMUS_OWNER_FINGERPRINT", owner.fingerprint)

        # The scripted model stands in for the provider; everything else — the
        # Gate, the ledger, the remote plane, the shell — is the real thing.
        monkeypatch.setattr(
            adapter, "LiteLLM",
            lambda **kw: ScriptedLLM([
                _reply(("write_file", {"path": "answer.txt", "content": "42\n"}),
                       text="Writing the answer."),
                _reply(("bash", {"command": "cat answer.txt"}), text="Checking."),
                _reply(("finish", {"summary": "wrote and verified answer.txt"})),
            ]),
        )

        logs_dir = tmp_path / "agent"
        agent = AgentFactory.create_agent_from_import_path(
            "optimus.adapters.harbor:OptimusAgent",
            logs_dir=logs_dir,
            model_name="test/scripted",
        )
        agent.session_id = "trial__abc__agent"
        environment = FakeEnvironment(BASH, workspace)
        context = AgentContext()

        try:
            asyncio.run(_drive(agent, environment, context))

            # The effect landed in the workspace.
            check = subprocess.run(
                [BASH, "-lc", f"cat {shlex.quote(workspace)}/answer.txt"],
                capture_output=True, text=True,
            )
            assert check.stdout.strip() == "42"

            # Harbor's own accounting was populated.
            assert context.n_input_tokens and context.n_output_tokens
            assert context.metadata["optimus"]["stop_reason"] == "finished"
            assert context.metadata["optimus"]["envelope"].startswith("env_")
            assert context.metadata["optimus"]["isolation"] == "CONTAINER"

            # And the three artefacts a trial ships with.
            metrics = json.loads((logs_dir / "optimus-metrics.json").read_text())
            assert metrics["turns"] == 3
            assert metrics["operator_interventions_required"] == 0
            assert (logs_dir / "ledger.db").exists()

            from harbor.models.trajectories import Trajectory

            Trajectory.model_validate(
                json.loads((logs_dir / "trajectory.json").read_text())
            )
        finally:
            subprocess.run(
                [BASH, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )

    @needs_bash
    def test_the_adapter_routes_over_the_manifest_local_first(
        self, tmp_path, monkeypatch
    ):
        """The branch two real bugs hid in.

        The other adapter tests pass a model name that is not declared, so they
        take the direct-client path and never construct a router. Both an
        UnboundLocalError and a missing import shipped through that gap and were
        found by running a container, not by the suite.
        """
        from harbor.models.agent.context import AgentContext

        import optimus.adapters.harbor as adapter
        from optimus.adapters.harbor import OptimusAgent

        manifest = tmp_path / "engines.toml"
        manifest.write_text(
            "\n".join([
                "[[engine]]",
                'id = "local"',
                "local = true",
                'base_url = "http://127.0.0.1:1/v1"',
                "[[model]]",
                'id = "tiny"',
                'engine = "local"',
                "context_tokens = 4096",
            ])
        )
        workspace = f"/tmp/optimus-trial-{uuid.uuid4().hex[:8]}"
        subprocess.run([BASH, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)

        owner = OwnerKey.generate()
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(json.dumps(
            issue(owner, principal="t", actor="agent", workspace=workspace,
                  venues=("harbor",), max_actions=20).to_dict()
        ))
        monkeypatch.setenv("OPTIMUS_ENVELOPE", str(envelope_path))
        monkeypatch.setenv("OPTIMUS_OWNER_FINGERPRINT", owner.fingerprint)
        monkeypatch.setenv("OPTIMUS_ENGINES", str(manifest))
        monkeypatch.delenv("OPTIMUS_ALLOW_REMOTE", raising=False)
        # Do not spend six real minutes proving the backoff works.
        monkeypatch.setenv("OPTIMUS_MAX_TRANSIENT_ERRORS", "2")
        monkeypatch.setenv("OPTIMUS_RETRY_BACKOFF_S", "0.01")
        monkeypatch.setenv("OPTIMUS_MAX_BACKOFF_S", "0.02")

        # The engine is declared but nothing listens on port 1, so the router
        # takes its unhealthy path — which is what must not crash.
        agent = OptimusAgent(logs_dir=tmp_path / "agent", model_name="tiny")
        context = AgentContext()
        try:
            asyncio.run(_drive(agent, FakeEnvironment(BASH, workspace), context))
            metrics = context.metadata["optimus"]
            # A receipt exists even though no model was reachable. That is the
            # point: the trial reports, rather than vanishing.
            assert metrics["stop_reason"] in ("provider_error", "provider_unavailable")
            assert (tmp_path / "agent" / "optimus-metrics.json").is_file()
            assert (tmp_path / "agent" / "ledger.db").is_file()
        finally:
            subprocess.run(
                [BASH, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )

    def test_a_lost_receipt_is_rebuilt_from_the_ledger(self, tmp_path):
        """Harbor calls `populate_context_post_run` even when `run()` was killed.

        A real trial was cut off at 900 seconds and lost every number, while the
        signed ledger on disk still held all of them. That the receipt can be
        rebuilt from the ledger alone is invariant 2 paying for itself.
        """
        from harbor.models.agent.context import AgentContext

        from optimus.adapters.harbor import METRICS_FILE, OptimusAgent
        from optimus.ledger.store import DurableChain, LedgerStore

        logs = tmp_path / "agent"
        logs.mkdir()
        # Build a ledger the way a real run would, then throw the run away.
        store = LedgerStore(logs / "ledger.db")
        gate = Gate(
            DurableChain(AgentKey.generate(), store), benchmark_policy(),
            RemoteResolver("/app", venue="harbor"), run_id="trial__x",
        )
        owner = OwnerKey.generate()
        gate.open_envelope(
            _envelope(owner, workspace=ANY_WORKSPACE),
            owner_fingerprint=owner.fingerprint,
        )
        venue = RemoteVenue(RecordingTransport(), name="harbor")
        tools = RemoteTools(gate=gate, venue=venue, workspace="/app")
        loop = AgentLoop(
            gate=gate, tools=tools, window=ContextWindow(ContextBudget(total=8_000)),
            llm=ScriptedLLM([
                _reply(("bash", {"command": "ls"}),
                       usage=Usage(input_tokens=900, output_tokens=40, cached_tokens=700)),
            ]),
            limits=LoopLimits(max_turns=1), run_id="trial__x",
        )
        loop.run("do the thing")
        store.close()
        assert not (logs / METRICS_FILE).exists()

        agent = OptimusAgent(logs_dir=logs, model_name="qwen35-9b")
        context = AgentContext()
        agent.populate_context_post_run(context)

        metrics = context.metadata["optimus"]
        assert metrics["input_tokens"] == 900
        assert metrics["output_tokens"] == 40
        assert metrics["cached_tokens"] == 700
        assert metrics["actions"] >= 1
        assert context.n_input_tokens == 900
        assert (logs / METRICS_FILE).is_file()

    @needs_bash
    def test_without_an_envelope_the_run_reports_the_interventions(
        self, tmp_path, monkeypatch
    ):
        """An unauthorised run is a legitimate row, and says what it cost."""
        from harbor.models.agent.context import AgentContext

        import optimus.adapters.harbor as adapter
        from optimus.adapters.harbor import OptimusAgent

        workspace = f"/tmp/optimus-trial-{uuid.uuid4().hex[:8]}"
        subprocess.run([BASH, "-lc", f"mkdir -p {shlex.quote(workspace)}"], check=True)
        monkeypatch.delenv("OPTIMUS_ENVELOPE", raising=False)
        monkeypatch.setattr(
            adapter, "LiteLLM",
            lambda **kw: ScriptedLLM([
                _reply(("write_file", {"path": "answer.txt", "content": "42"})),
                _reply(("finish", {"summary": "tried"})),
            ]),
        )

        agent = OptimusAgent(logs_dir=tmp_path / "agent", model_name="test/scripted")
        context = AgentContext()
        try:
            asyncio.run(_drive(agent, FakeEnvironment(BASH, workspace), context))
            metrics = context.metadata["optimus"]
            assert metrics["envelope"] is None
            assert metrics["operator_interventions_required"] >= 1
            # And nothing was written, because nothing was authorised.
            check = subprocess.run(
                [BASH, "-lc", f"ls {shlex.quote(workspace)}"],
                capture_output=True, text=True,
            )
            assert "answer.txt" not in check.stdout
        finally:
            subprocess.run(
                [BASH, "-lc", f"rm -rf {shlex.quote(workspace)}"], check=False
            )


async def _drive(agent, environment, context) -> None:
    await agent.setup(environment)
    await agent.run("write 42 into answer.txt", environment, context)


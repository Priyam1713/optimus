"""Run Optimus on Terminal-Bench through Harbor, and print the row.

Wraps the four steps that have to line up — envelope, key, harbor, report — so
none of them can be forgotten and none of them has to be retyped.

**This script never reads the provider key.** It checks that `state/harbor.env`
exists and is non-empty, then hands the *path* to Harbor's `--env-file`. The
value goes from that file into Harbor's process and nowhere else; nothing here,
and nothing in the ledger, ever sees it.

    python scripts/bench.py --tasks 2                  # smoke run, pennies
    python scripts/bench.py --tasks 89 --attempts 5    # the board row

The envelope's owner fingerprint is read back out of the envelope file rather
than passed in, because the Gate refuses an envelope checked against the key it
carries — the fingerprint has to arrive from somewhere the document does not
control, and here that is the operator's own `state/owner.key`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_ENVELOPE = ROOT / "state" / "envelope.json"
DEFAULT_ENV_FILE = ROOT / "state" / "harbor.env"
DEFAULT_OWNER = ROOT / "state" / "owner.key"


def _fingerprint_from_owner_key(path: Path) -> str:
    """Derive the expected fingerprint from the operator's own key file.

    Deliberately not taken from the envelope: `Envelope.verify` exists to catch a
    document signed by the wrong key, and it cannot do that if the thing it is
    checked against came out of the same document.
    """
    from optimus.ledger.keys import OwnerKey

    return OwnerKey.load(path).fingerprint


def preflight(args: argparse.Namespace) -> list[str]:
    """Everything that must be true before a single dollar is spent."""
    problems: list[str] = []

    envelope_path = Path(args.envelope)
    reissue = (
        f"    optimus envelope --owner {DEFAULT_OWNER} --principal you "
        f"--any-workspace --venue harbor --isolation CONTAINER"
    )
    if not envelope_path.is_file():
        problems.append(f"no envelope at {envelope_path}. Issue one:\n{reissue}")
    else:
        # Checked here as well as at the Gate, because the cost of finding out
        # late is a whole run: an expired envelope refused all 31 actions of a
        # real trial, correctly, after nine minutes of local inference.
        import json
        from optimus.gate.envelope import Envelope
        from optimus.ledger.chain import now_ms

        try:
            envelope = Envelope.from_dict(
                json.loads(envelope_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"{envelope_path} is not a readable envelope: {exc}")
        else:
            remaining_min = (envelope.expires_ms - now_ms()) // 60_000
            if remaining_min <= 0:
                problems.append(
                    f"envelope {envelope.envelope_id} expired "
                    f"{-remaining_min} minutes ago. Issue a fresh one:\n{reissue}"
                )
            elif remaining_min < 30:
                problems.append(
                    f"envelope {envelope.envelope_id} expires in {remaining_min} "
                    f"minutes, which will not outlast this run. Issue a fresh one:\n"
                    f"{reissue}"
                )

    # A provider key is needed only when a run is allowed to leave the machine.
    # Local-first means the ordinary path has no key at all.
    env_file = Path(args.env_file)
    if args.allow_remote:
        if not env_file.is_file():
            problems.append(
                f"--allow-remote, but no provider key file at {env_file}. Write "
                "one yourself, or drop --allow-remote and route locally."
            )
        elif env_file.stat().st_size < 8:
            problems.append(f"{env_file} looks empty")

    if not Path(args.owner).is_file():
        problems.append(f"no owner key at {args.owner}; run `optimus keygen`")

    if subprocess.run(
        ["docker", "info"], capture_output=True, check=False
    ).returncode != 0:
        problems.append("docker is not reachable; start it and retry")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="terminal-bench@2.0")
    parser.add_argument("--model", default="",
                        help="a declared model id from the engine manifest, or a "
                             "literal provider string. Empty routes over the "
                             "manifest with local engines first.")
    parser.add_argument("--engines", default=str(ROOT / "configs" / "engines.toml"))
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit hosted engines. Off by default: this is a "
                             "local-first system, and leaving the machine is a "
                             "decision rather than a fallback.")
    parser.add_argument("--tasks", type=int, default=2,
                        help="how many tasks (default 2: a smoke run)")
    parser.add_argument("--attempts", type=int, default=1,
                        help="trials per task; pass^k needs at least 2")
    parser.add_argument("--concurrent", type=int, default=2)
    parser.add_argument("--job-name", default="")
    parser.add_argument("--jobs-dir", default=str(ROOT / "jobs"))
    parser.add_argument("--envelope", default=str(DEFAULT_ENVELOPE))
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--owner", default=str(DEFAULT_OWNER))
    parser.add_argument("--max-cost-usd", type=float, default=1.0,
                        help="per-trial ceiling enforced by the loop, not the model")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--timeout-multiplier", type=float, default=1.0,
                        help="stretch Harbor's per-task timeouts. Local models "
                             "need this: a 9B doing 30 turns takes far longer "
                             "than a hosted model doing the same work.")
    parser.add_argument("--max-wall-s", type=float, default=0.0,
                        help="the loop's own ceiling. Keep it under Harbor's "
                             "agent timeout so the loop stops itself and writes "
                             "a receipt, rather than being cut off mid-turn.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the command and the preflight, run nothing")
    args = parser.parse_args()

    problems = preflight(args)
    if problems:
        print("preflight failed:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        return 2

    fingerprint = _fingerprint_from_owner_key(Path(args.owner))
    envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
    print(f"envelope {envelope['envelope_id']}: {envelope['actor']} may "
          f"{', '.join(envelope['verbs'])} in "
          f"{'any path' if envelope['workspace'] == '*' else envelope['workspace']} "
          f"of {', '.join(envelope['venues'])}; "
          f"{envelope['max_actions']} actions")
    print(f"owner fingerprint {fingerprint} (from {args.owner}, not from the envelope)")
    from optimus.loop.engines import Registry

    registry = Registry.load(args.engines)
    candidates, excluded = registry.candidates(
        allow_remote=args.allow_remote, model_id=args.model
    )
    if not candidates:
        print(f"nothing routable from {registry.source}:", file=sys.stderr)
        for reason in excluded:
            print(f"  - {reason}", file=sys.stderr)
        return 2
    print("route: " + " -> ".join(c.label for c in candidates)
          + ("  (local only)" if not args.allow_remote else "  (remote permitted)"))
    for reason in excluded:
        print(f"  excluded: {reason}")
    print(f"{args.tasks} task(s) x {args.attempts} attempt(s), "
          f"capped at ${args.max_cost_usd:.2f}/trial\n")

    job_name = args.job_name or f"optimus-{args.tasks}x{args.attempts}"
    command = [
        str(ROOT / ".venv" / "Scripts" / "harbor.exe")
        if os.name == "nt" else "harbor",
        "run",
        "-d", args.dataset,
        "--agent", "optimus.adapters.harbor:OptimusAgent",
        # Harbor needs a model label for its own result grouping; the router
        # decides what actually serves each turn.
        "-m", args.model or candidates[0].model.id,
        "-l", str(args.tasks),
        "-k", str(args.attempts),
        "-n", str(args.concurrent),
        "-o", args.jobs_dir,
        "--job-name", job_name,
        # Passed to the agent inside its own environment, never onto a shell
        # line: the loop's ceilings are what stop a runaway trial, and they are
        # set here rather than trusted to the model.
        "--ae", f"OPTIMUS_ENVELOPE={args.envelope}",
        "--ae", f"OPTIMUS_OWNER_FINGERPRINT={fingerprint}",
        "--ae", f"OPTIMUS_MAX_COST_USD={args.max_cost_usd}",
        "--ae", f"OPTIMUS_MAX_TURNS={args.max_turns}",
        "--ae", f"OPTIMUS_ENGINES={args.engines}",
    ]
    if args.timeout_multiplier != 1.0:
        command += ["--timeout-multiplier", str(args.timeout_multiplier)]
    if args.max_wall_s:
        command += ["--ae", f"OPTIMUS_MAX_WALL_S={args.max_wall_s}"]
    if args.allow_remote:
        command += [
            "--env-file", str(args.env_file), "--ae", "OPTIMUS_ALLOW_REMOTE=1",
        ]

    print("$ " + " ".join(command) + "\n")
    if args.dry_run:
        return 0

    # The adapter runs on the host, so it reads these from this process. `--ae`
    # covers the case where a future transport moves it into the environment.
    child_env = {
        **os.environ,
        "OPTIMUS_ENVELOPE": str(args.envelope),
        "OPTIMUS_OWNER_FINGERPRINT": fingerprint,
        "OPTIMUS_MAX_COST_USD": str(args.max_cost_usd),
        "OPTIMUS_MAX_TURNS": str(args.max_turns),
        "OPTIMUS_ENGINES": str(args.engines),
        "OPTIMUS_ALLOW_REMOTE": "1" if args.allow_remote else "",
        **({"OPTIMUS_MAX_WALL_S": str(args.max_wall_s)} if args.max_wall_s else {}),
    }
    result = subprocess.run(command, env=child_env, check=False)

    job_dir = Path(args.jobs_dir) / job_name
    print(f"\n{'=' * 70}\nthe row\n{'=' * 70}")
    if not job_dir.exists():
        candidates = sorted(
            Path(args.jobs_dir).glob("*"), key=lambda p: p.stat().st_mtime
        )
        job_dir = candidates[-1] if candidates else job_dir
    subprocess.run(
        [sys.executable, "-m", "optimus.cli", "report", str(job_dir)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=False,
    )
    print(f"\ntrials under {job_dir}")
    print("each trial's signed ledger is at <trial>/agent/ledger.db; verify one with")
    print(f"  optimus verify --ledger <trial>/agent/ledger.db "
          f"--owner-fingerprint {fingerprint}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""Engines and models, as data — and local ones first.

M3 shipped a model layer with one model in it, named by a provider-prefixed
string. That is brand-first, and it is the wrong shape for a local-first system:
it makes a hosted API the reference implementation and local inference the
special case.

The design here is ported from Achilles (`E:\\Test\\achilles-work`,
`src/sovereign_ai/inference/broker.py` and `resources/scheduler.py`), whose
governing sentence is worth keeping verbatim: **"Routes capabilities, not
brands. Engines remain replaceable adapters."** Three of its ideas are taken
directly:

1. **Engines and models are declared data, not code.** Adding a backend is a
   config edit, not a new client class.
2. **Local-first is a structural gate, not a default argument.** A remote engine
   is excluded from candidacy unless the caller explicitly opts in — Achilles's
   `_eligible` does exactly this and it is the right place for the rule, because
   a default can be overridden by accident and a filter cannot.
3. **Runtime truth beats the manifest.** A configured engine that is unhealthy
   is skipped rather than allowed to turn a good routing decision into a failed
   request.

**What is deliberately not ported yet, and why.** Achilles carries a weighted
scorer over quality priors, measured latency, reliability and VRAM fit. Its own
docstring records that 84 of its 89 capabilities have exactly one eligible
candidate once the filters run, so the scorer rarely decides anything. Optimus
today has one capability — drive the agent loop — and a handful of models on one
engine. Porting the scorer now would be building machinery for a decision that
does not yet exist; ordering is by declared preference, and the scorer arrives
when there is benchmark data to feed it. Likewise the GPU arbiter: the
llama.cpp router already does residency and VRAM fitting (`--fit`,
`--sleep-idle-seconds`), and a second arbiter above one that already works would
be two things to disagree.

**TOML rather than Achilles's YAML**, because `tomllib` is in the standard
library and this package's only runtime dependency is a crypto library. Adding
PyYAML to read one config file is not a trade worth making.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

#: Where a config is looked for when none is named.
DEFAULT_CONFIG_PATHS: tuple[str, ...] = (
    "configs/engines.toml",
    "~/.optimus/engines.toml",
)


class ConfigError(Exception):
    """A malformed engine manifest. Always fatal: a router that silently drops a
    misspelled engine is a router that quietly stops being local-first."""


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """One backend that can serve a chat completion."""

    id: str
    #: `openai_compatible` — anything speaking the OpenAI HTTP API, which is
    #: llama.cpp, vLLM, SGLang, Ollama and every hosted provider worth naming.
    kind: str = "openai_compatible"
    enabled: bool = True
    #: **The local-first gate.** False means this engine leaves the machine, and
    #: it is excluded from routing unless a caller sets `allow_remote`.
    local: bool = True
    base_url: str = ""
    health_url: str = ""
    #: Environment variable holding the credential. The value is never read into
    #: config, never logged, and never written to the ledger — only the name is.
    api_key_env: str = ""
    #: Prefix litellm needs to pick a provider. Local OpenAI-compatible servers
    #: use `openai/` plus an `api_base`.
    litellm_prefix: str = "openai/"
    timeout_s: float = 600.0
    #: Ordering hint. Lower goes first among otherwise-equal engines.
    priority: int = 100
    notes: str = ""

    @property
    def remote(self) -> bool:
        return not self.local

    def api_key(self) -> str | None:
        """Read the credential at call time, from the environment only."""
        if not self.api_key_env:
            # llama.cpp and friends want *something* in the header and ignore it.
            return "local" if self.local else None
        return os.environ.get(self.api_key_env)

    def usable(self) -> tuple[bool, str]:
        """Can this engine be called at all? Says why not, when not."""
        if not self.enabled:
            return False, "disabled in config"
        if self.kind != "openai_compatible":
            return False, f"unsupported engine kind {self.kind!r}"
        if not self.base_url:
            return False, "no base_url"
        if self.remote and not self.api_key():
            # Achilles's honest failure: a remote engine may be declared without
            # ever being usable, and it should say so at selection time rather
            # than fail obscurely at call time.
            return False, f"no credential in ${self.api_key_env or '(unset)'}"
        return True, ""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One model, on one engine."""

    id: str
    engine: str
    #: The id the engine itself knows. Defaults to `id`.
    served_as: str = ""
    context_tokens: int = 32_768
    max_output_tokens: int = 8_000
    #: The loop drives everything through tool calls, so a model that cannot
    #: emit them cannot serve it. An eligibility filter, not a preference.
    supports_tools: bool = True
    #: Lower goes first. Preference, not a learned score — see the module note.
    priority: int = 100
    notes: str = ""

    @property
    def runtime_id(self) -> str:
        return self.served_as or self.id


@dataclass(frozen=True, slots=True)
class Candidate:
    """One (engine, model) pair the router may try."""

    engine: EngineSpec
    model: ModelSpec

    @property
    def label(self) -> str:
        return f"{self.engine.id}:{self.model.id}"

    @property
    def litellm_model(self) -> str:
        return f"{self.engine.litellm_prefix}{self.model.runtime_id}"


@dataclass
class Registry:
    """Engines and models, and the local-first rule over them."""

    engines: dict[str, EngineSpec] = field(default_factory=dict)
    models: list[ModelSpec] = field(default_factory=list)
    source: str = ""

    # -- loading --------------------------------------------------------------

    @staticmethod
    def load(path: str | os.PathLike[str] | None = None) -> "Registry":
        resolved = _resolve_config(path)
        if resolved is None:
            raise ConfigError(
                "no engine manifest found. Looked for: "
                + ", ".join(DEFAULT_CONFIG_PATHS)
                + ". Write one, or pass --engines/OPTIMUS_ENGINES."
            )
        try:
            raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"{resolved}: {exc}") from exc
        return Registry.from_dict(raw, source=str(resolved))

    @staticmethod
    def from_dict(raw: dict[str, Any], *, source: str = "") -> "Registry":
        engines: dict[str, EngineSpec] = {}
        for entry in raw.get("engine", []):
            spec = _build(EngineSpec, entry, "engine")
            if spec.id in engines:
                raise ConfigError(f"duplicate engine id {spec.id!r}")
            engines[spec.id] = spec

        models: list[ModelSpec] = []
        for entry in raw.get("model", []):
            spec = _build(ModelSpec, entry, "model")
            if spec.engine not in engines:
                raise ConfigError(
                    f"model {spec.id!r} names engine {spec.engine!r}, which is not declared"
                )
            models.append(spec)

        if not engines:
            raise ConfigError("manifest declares no engines")
        return Registry(engines=engines, models=models, source=source)

    # -- routing --------------------------------------------------------------

    def candidates(
        self,
        *,
        allow_remote: bool = False,
        model_id: str = "",
        needs_tools: bool = True,
    ) -> tuple[list[Candidate], list[str]]:
        """Every pair worth trying, best first, plus why the rest were excluded.

        The exclusion list is returned rather than logged because "why did it not
        use the local one" is the question this layer will be asked most often,
        and an answer that only exists in a log file is not an answer.
        """
        chosen: list[Candidate] = []
        excluded: list[str] = []

        for model in self.models:
            if model_id and model.id != model_id:
                continue
            if needs_tools and not model.supports_tools:
                excluded.append(f"{model.id}: does not support tool calls")
                continue
            engine = self.engines[model.engine]
            if engine.remote and not allow_remote:
                # The gate. A default could be overridden by accident; a filter
                # has to be opted through.
                excluded.append(
                    f"{engine.id}:{model.id}: remote engine, and this request is "
                    "local-only (pass allow_remote to opt in)"
                )
                continue
            ok, why_not = engine.usable()
            if not ok:
                excluded.append(f"{engine.id}:{model.id}: {why_not}")
                continue
            chosen.append(Candidate(engine=engine, model=model))

        # Local before remote, then declared priority, then declaration order.
        chosen.sort(key=lambda c: (c.engine.remote, c.engine.priority, c.model.priority))
        if model_id and not chosen and not excluded:
            excluded.append(f"no model declared with id {model_id!r}")
        return chosen, excluded

    def __iter__(self) -> Iterator[ModelSpec]:
        return iter(self.models)

    def describe(self) -> str:
        lines = [f"engines ({self.source or 'in memory'}):"]
        for engine in sorted(self.engines.values(), key=lambda e: (e.remote, e.priority)):
            ok, why_not = engine.usable()
            where = "local" if engine.local else "REMOTE"
            lines.append(
                f"  {engine.id:16} {where:6} {'ok' if ok else 'unusable: ' + why_not}"
                f"  {engine.base_url}"
            )
        lines.append("models:")
        for model in sorted(self.models, key=lambda m: (m.engine, m.priority)):
            tools = "tools" if model.supports_tools else "NO TOOLS"
            lines.append(
                f"  {model.id:28} on {model.engine:14} {model.context_tokens:>7} ctx  {tools}"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _resolve_config(path: str | os.PathLike[str] | None) -> Path | None:
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"no engine manifest at {candidate}")
        return candidate
    from_env = os.environ.get("OPTIMUS_ENGINES")
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"OPTIMUS_ENGINES points at {candidate}, which is not a file")
        return candidate
    for default in DEFAULT_CONFIG_PATHS:
        candidate = Path(default).expanduser()
        if candidate.is_file():
            return candidate
    return None


def _build(cls: type, entry: Any, label: str):
    """Construct a spec, refusing unknown keys.

    A typo in a manifest must be an error. Silently ignoring `enable = false`
    because the field is spelled `enabled` is the config-file version of a deny
    rule that does not match — the bug `policy.py` was written to prevent.
    """
    if not isinstance(entry, dict):
        raise ConfigError(f"[[{label}]] entries must be tables, got {type(entry).__name__}")
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(entry) - known
    if unknown:
        raise ConfigError(
            f"[[{label}]] has unknown key(s) {sorted(unknown)}; known keys are {sorted(known)}"
        )
    try:
        return cls(**entry)
    except TypeError as exc:
        raise ConfigError(f"[[{label}]]: {exc}") from exc


def default_registry(paths: Sequence[str] = DEFAULT_CONFIG_PATHS) -> Registry | None:
    """Best-effort load, for callers that can proceed without one."""
    try:
        return Registry.load()
    except ConfigError:
        return None

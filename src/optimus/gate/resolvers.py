"""Request -> resolved target. The only place raw specs are interpreted."""

from __future__ import annotations

import os
from typing import Any

from .targets import (
    OpaqueTarget,
    ResolvedTarget,
    TargetRefused,
    resolve_argv,
    resolve_fs,
    resolve_url,
)
from .types import CapabilityRequest, Verb


class WorkspaceResolver:
    """Resolves against one workspace root, refusing anything outside it."""

    def __init__(self, workspace: str | os.PathLike[str], *, allow_private_net: bool = False):
        self.workspace = os.fspath(workspace)
        self.allow_private_net = allow_private_net

    def __call__(self, req: CapabilityRequest) -> ResolvedTarget:
        spec: Any = req.target_spec

        match req.verb:
            case Verb.READ | Verb.WRITE | Verb.DELETE:
                if not isinstance(spec, str):
                    raise TargetRefused(f"{req.verb} needs a path string, got {type(spec).__name__}")
                return resolve_fs(spec, self.workspace, must_exist=req.verb is Verb.READ)

            case Verb.EXECUTE:
                if isinstance(spec, str):
                    # A shell string is not an argv. Accepting one would hand
                    # quoting decisions to whoever wrote the string.
                    raise TargetRefused("execute needs an argv list, not a shell string")
                return resolve_argv(spec, self.workspace)

            case Verb.NAVIGATE | Verb.NETWORK_SEND:
                if not isinstance(spec, str):
                    raise TargetRefused("network verbs need a url string")
                return resolve_url(spec, allow_private=self.allow_private_net)

            case _:
                # Surfaces the Gate cannot yet inspect structurally still get an
                # identity so policy can speak about them.
                if isinstance(spec, dict) and "namespace" in spec and "identity" in spec:
                    return OpaqueTarget(
                        kind="opaque",
                        namespace=str(spec["namespace"]),
                        identity=str(spec["identity"]),
                        detail={k: v for k, v in spec.items() if k not in {"namespace", "identity"}},
                    )
                raise TargetRefused(
                    f"no resolver for verb {req.verb}; opaque targets need "
                    "{'namespace': ..., 'identity': ...}"
                )

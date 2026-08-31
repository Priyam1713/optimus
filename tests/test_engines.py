"""The local-first model layer.

The gate in `Registry.candidates` is the whole reason this layer exists: a
local-first system that reaches a hosted API by omission is not local-first, it
is a hosted system with a local option. So most of these tests are about what
must *not* be routable, in the same spirit as the envelope tests.
"""

from __future__ import annotations

import pytest

from optimus.loop.engines import ConfigError, EngineSpec, ModelSpec, Registry
from optimus.loop.llm import ModelReply, Usage
from optimus.loop.router import RoutedLLM

MANIFEST = """
[[engine]]
id = "llama_cpp"
local = true
base_url = "http://127.0.0.1:18080/v1"
priority = 10

[[engine]]
id = "cloud"
local = false
base_url = "https://api.example.com/v1"
api_key_env = "TEST_CLOUD_KEY"
litellm_prefix = "cloud/"
priority = 200

[[model]]
id = "qwen35-9b"
engine = "llama_cpp"
context_tokens = 32768
priority = 10

[[model]]
id = "qwen38-27b"
engine = "llama_cpp"
context_tokens = 16384
priority = 20

[[model]]
id = "cloud-big"
engine = "cloud"
context_tokens = 1000000
priority = 1
"""


def _registry(text: str = MANIFEST) -> Registry:
    import tomllib

    return Registry.from_dict(tomllib.loads(text), source="test")


class TestManifest:
    def test_a_typo_is_an_error_not_a_silent_default(self):
        """The config-file version of a deny rule that does not match."""
        with pytest.raises(ConfigError, match="unknown key"):
            _registry('[[engine]]\nid = "x"\nenable = true\n')

    def test_a_model_naming_an_undeclared_engine_is_refused(self):
        with pytest.raises(ConfigError, match="not declared"):
            _registry('[[engine]]\nid = "a"\n[[model]]\nid = "m"\nengine = "ghost"\n')

    def test_duplicate_engine_ids_are_refused(self):
        with pytest.raises(ConfigError, match="duplicate"):
            _registry('[[engine]]\nid = "a"\n[[engine]]\nid = "a"\n')

    def test_a_manifest_with_no_engines_is_refused(self):
        with pytest.raises(ConfigError, match="no engines"):
            _registry("")

    def test_a_missing_manifest_names_where_it_looked(self):
        with pytest.raises(ConfigError, match="no engine manifest at"):
            Registry.load("does/not/exist.toml")


class TestLocalFirstGate:
    def test_remote_engines_are_excluded_by_default(self):
        candidates, excluded = _registry().candidates()
        assert [c.label for c in candidates] == [
            "llama_cpp:qwen35-9b", "llama_cpp:qwen38-27b"
        ]
        assert any("local-only" in reason for reason in excluded)

    def test_the_gate_is_a_filter_not_a_default(self, monkeypatch):
        """Opting in is the only way past it, and even then local goes first."""
        monkeypatch.setenv("TEST_CLOUD_KEY", "x")
        candidates, _ = _registry().candidates(allow_remote=True)
        labels = [c.label for c in candidates]
        assert "cloud:cloud-big" in labels
        # `cloud-big` has the best declared priority of any model in the file.
        # It still comes last, because local-before-remote outranks priority.
        assert labels.index("cloud:cloud-big") == len(labels) - 1
        assert labels[0] == "llama_cpp:qwen35-9b"

    def test_a_remote_engine_without_a_credential_is_unusable(self, monkeypatch):
        """Achilles's honest failure: declared, and never silently usable."""
        monkeypatch.delenv("TEST_CLOUD_KEY", raising=False)
        candidates, excluded = _registry().candidates(allow_remote=True)
        assert all(c.engine.local for c in candidates)
        assert any("TEST_CLOUD_KEY" in reason for reason in excluded)

    def test_a_model_without_tool_calling_cannot_serve_the_loop(self):
        registry = _registry()
        registry.models.append(
            ModelSpec(id="text-only", engine="llama_cpp", supports_tools=False)
        )
        candidates, excluded = registry.candidates()
        assert "llama_cpp:text-only" not in [c.label for c in candidates]
        assert any("does not support tool calls" in r for r in excluded)

    def test_a_disabled_engine_is_excluded_with_a_reason(self):
        registry = _registry()
        registry.engines["llama_cpp"] = EngineSpec(
            id="llama_cpp", enabled=False, base_url="http://x/v1"
        )
        _, excluded = registry.candidates()
        assert any("disabled in config" in r for r in excluded)

    def test_pinning_a_model_id_narrows_the_route(self):
        candidates, _ = _registry().candidates(model_id="qwen38-27b")
        assert [c.label for c in candidates] == ["llama_cpp:qwen38-27b"]

    def test_pinning_an_unknown_id_says_so(self):
        candidates, excluded = _registry().candidates(model_id="nope")
        assert not candidates
        assert any("nope" in r for r in excluded)

    def test_a_local_engine_needs_no_credential(self):
        engine = _registry().engines["llama_cpp"]
        assert engine.usable()[0] is True
        assert engine.api_key() == "local"


class TestRouting:
    def _router(self, replies, *, allow_remote=False, healthy=True):
        rows: list[tuple[str, dict]] = []
        router = RoutedLLM(
            registry=_registry(), allow_remote=allow_remote,
            record=lambda k, p: rows.append((k, p)),
        )
        calls: list[str] = []

        class FakeClient:
            def __init__(self, label):
                self.label = label

            def complete(self, messages, tools):
                calls.append(self.label)
                return replies.pop(0)

        router._client = lambda c: FakeClient(c.label)  # type: ignore[assignment]
        import optimus.loop.router as module

        self._patch = module
        module.check_health = lambda c, timeout_s=5.0: module.HealthResult(healthy, "")
        return router, rows, calls

    def test_the_first_healthy_candidate_serves_and_is_recorded(self):
        router, rows, calls = self._router([ModelReply(text="hi", usage=Usage())])
        reply = router.complete([], [])
        assert not reply.error and calls == ["llama_cpp:qwen35-9b"]
        route = next(p for k, p in rows if k == "model.route")
        assert route["engine"] == "llama_cpp" and route["local"] is True
        assert any("local-only" in r for r in route["excluded"])

    def test_a_transient_failure_falls_through_to_the_next_candidate(self):
        router, rows, calls = self._router([
            ModelReply(error="503", retryable=True, usage=Usage()),
            ModelReply(text="second one worked", usage=Usage()),
        ])
        reply = router.complete([], [])
        assert not reply.error
        assert calls == ["llama_cpp:qwen35-9b", "llama_cpp:qwen38-27b"]
        route = next(p for k, p in rows if k == "model.route")
        assert route["model"] == "qwen38-27b"
        assert route["fallbacks"] and "503" in route["fallbacks"][0]

    def test_a_fatal_failure_does_not_fall_through(self):
        """A malformed request is malformed on every candidate."""
        router, _, calls = self._router([
            ModelReply(error="BadRequest", retryable=False, usage=Usage()),
            ModelReply(text="never reached", usage=Usage()),
        ])
        reply = router.complete([], [])
        assert reply.error and calls == ["llama_cpp:qwen35-9b"]
        assert reply.retryable is False

    def test_an_unhealthy_engine_is_skipped_not_attempted(self):
        router, _rows, calls = self._router([], healthy=False)
        reply = router.complete([], [])
        assert calls == []
        assert "unhealthy" in reply.error

    def test_exhausting_every_candidate_reports_all_of_them(self):
        router, _, _ = self._router([
            ModelReply(error="503 one", retryable=True, usage=Usage()),
            ModelReply(error="503 two", retryable=True, usage=Usage()),
        ])
        reply = router.complete([], [])
        assert "503 one" in reply.error and "503 two" in reply.error
        # Still transient, so the loop's own backoff will wait it out.
        assert reply.retryable is True

    def test_no_routable_model_is_fatal_rather_than_retried_forever(self):
        router = RoutedLLM(registry=_registry(), allow_remote=False, model_id="ghost")
        reply = router.complete([], [])
        assert reply.error.startswith("no routable model")
        assert reply.retryable is False

    def test_a_working_candidate_is_reused_without_re_probing_health(self):
        router, rows, _calls = self._router([
            ModelReply(text="one", usage=Usage()),
            ModelReply(text="two", usage=Usage()),
        ])
        router.complete([], [])
        probed: list[str] = []
        self._patch.check_health = lambda c, timeout_s=5.0: (
            probed.append(c.label) or self._patch.HealthResult(True, "")
        )
        router.complete([], [])
        assert probed == []
        # And the route is recorded once, not once per turn.
        assert len([k for k, _ in rows if k == "model.route"]) == 1

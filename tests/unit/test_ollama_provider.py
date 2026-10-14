"""OllamaProvider against httpx.MockTransport only -- no network in unit tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import structlog
from pydantic import BaseModel

from ken_contracts.model import (
    GenerationOptions,
    Message,
    ModelProvider,
    ModelProviderError,
    ModelRole,
    ModelSchemaViolationError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from ken_core.config import Settings
from ken_core.providers.model._text import (
    clean_structured_output,
    extract_json_object,
    strip_think,
)
from ken_core.providers.model.ollama import OllamaProvider

FAST = "fast-model:1b"
REASONING = "reasoning-model:8b"
EMBED = "embed-model"
MSGS = [Message(role="user", content="classify this")]

Handler = Callable[[httpx.Request], httpx.Response]


class Verdict(BaseModel):
    label: str
    score: float


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "model_provider": "ollama",
        "ollama_base_url": "http://ollama.test:11434",
        "model_fast": FAST,
        "model_reasoning": REASONING,
        "model_embedding": EMBED,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class Recorder:
    """Records request bodies and replays scripted responses per path."""

    def __init__(self, routes: dict[str, Handler | list[httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": "no route"})
        if isinstance(route, list):
            return route.pop(0)
        return route(request)


def _provider(recorder: Recorder, **overrides: object) -> OllamaProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return OllamaProvider(_settings(**overrides), client=client)


def _chat(content: str, model: str = FAST) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 11,
            "eval_count": 7,
        },
    )


def _tags(*models: tuple[str, list[str] | None]) -> httpx.Response:
    entries = []
    for name, caps in models:
        entry: dict[str, Any] = {"name": name, "model": name}
        if caps is not None:
            entry["capabilities"] = caps
        entries.append(entry)
    return httpx.Response(200, json={"models": entries})


VALID = json.dumps({"label": "relevant", "score": 0.75})


# ------------------------------------------------------------------ helpers
def test_strip_think_variants() -> None:
    assert strip_think("<think>hmm\nlong</think>\n answer ") == "answer"
    assert strip_think("<think>a</think>x<think>b</think>y") == "xy"
    assert strip_think("reasoning leaked</think>answer") == "answer"
    assert strip_think("answer<think>unterminated") == "answer"
    assert strip_think("plain") == "plain"


def test_extract_json_object() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json_object('Sure! {"a": {"b": 2}} done') == '{"a": {"b": 2}}'
    assert extract_json_object('{broken {"ok": true}') == '{"ok": true}'
    assert extract_json_object("no json here") is None
    assert extract_json_object("[1, 2]") is None


def test_clean_structured_output_flags_repair() -> None:
    assert clean_structured_output(f"  {VALID}\n") == (VALID, False)
    assert clean_structured_output(f"<think>x</think>{VALID}") == (VALID, True)


# ----------------------------------------------------------------- protocol
def test_conforms_to_protocol() -> None:
    assert isinstance(_provider(Recorder({})), ModelProvider)


# ----------------------------------------------------------------- complete
async def test_complete_request_body_and_think_stripping() -> None:
    rec = Recorder({"/api/chat": [_chat("<think>private</think>\nHello there")]})
    provider = _provider(rec)
    options = GenerationOptions(temperature=0.2, top_p=0.9, max_tokens=32, seed=7, stop=["\n\n"])
    result = await provider.complete(role=ModelRole.FAST, messages=MSGS, options=options)

    assert result.text == "Hello there"
    assert result.model == FAST
    assert result.role is ModelRole.FAST
    assert result.provider == "ollama"
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (11, 7)
    assert result.latency_ms >= 0

    [body] = rec.bodies("/api/chat")
    assert rec.requests[0].url == "http://ollama.test:11434/api/chat"
    assert body["model"] == FAST
    assert body["stream"] is False
    assert body["think"] is False
    assert "format" not in body
    assert body["messages"] == [{"role": "user", "content": "classify this"}]
    assert body["options"] == {
        "temperature": 0.2,
        "top_p": 0.9,
        "num_predict": 32,
        "seed": 7,
        "stop": ["\n\n"],
    }


async def test_role_resolves_to_configured_model() -> None:
    rec = Recorder({"/api/chat": [_chat("x", REASONING)]})
    await _provider(rec).complete(role=ModelRole.REASONING, messages=MSGS)
    assert rec.bodies("/api/chat")[0]["model"] == REASONING


async def test_default_options_send_only_temperature() -> None:
    rec = Recorder({"/api/chat": [_chat("x")]})
    await _provider(rec).complete(role=ModelRole.FAST, messages=MSGS)
    body = rec.bodies("/api/chat")[0]
    assert body["options"] == {"temperature": 0.0}
    assert body["think"] is False


# ------------------------------------------------------- complete_structured
async def test_structured_success_sends_schema_format() -> None:
    rec = Recorder({"/api/chat": [_chat(VALID)]})
    result = await _provider(rec).complete_structured(
        role=ModelRole.FAST, messages=MSGS, schema=Verdict
    )
    assert result.value == Verdict(label="relevant", score=0.75)
    assert result.attempts == 1
    assert result.repaired is False
    body = rec.bodies("/api/chat")[0]
    assert body["format"] == Verdict.model_json_schema()
    assert body["think"] is False


async def test_structured_repair_of_think_and_fence() -> None:
    raw = f"<think>let me think</think>\n```json\n{VALID}\n```"
    rec = Recorder({"/api/chat": [_chat(raw)]})
    result = await _provider(rec).complete_structured(
        role=ModelRole.FAST, messages=MSGS, schema=Verdict
    )
    assert result.value.label == "relevant"
    assert result.repaired is True
    assert result.attempts == 1
    assert result.completion.text == VALID


async def test_structured_retry_then_success() -> None:
    bad = json.dumps({"label": "relevant", "score": "high"})
    rec = Recorder({"/api/chat": [_chat(bad), _chat(VALID)]})
    result = await _provider(rec).complete_structured(
        role=ModelRole.REASONING, messages=MSGS, schema=Verdict, max_attempts=2
    )
    assert result.attempts == 2
    assert result.value.score == 0.75
    first, second = rec.bodies("/api/chat")
    assert first["messages"] == [{"role": "user", "content": "classify this"}]
    # The retry carries the failed output and validation feedback.
    assert len(second["messages"]) == 3
    assert second["messages"][1] == {"role": "assistant", "content": bad}
    assert second["messages"][2]["role"] == "user"
    assert "score" in second["messages"][2]["content"]
    assert second["model"] == REASONING


async def test_structured_retry_exhaustion_raises_with_raw_text() -> None:
    rec = Recorder({"/api/chat": [_chat("not json"), _chat("<think>x</think>still not json")]})
    with pytest.raises(ModelSchemaViolationError) as info:
        await _provider(rec).complete_structured(
            role=ModelRole.FAST, messages=MSGS, schema=Verdict, max_attempts=2
        )
    assert info.value.attempts == 2
    assert info.value.raw_text == "<think>x</think>still not json"
    assert len(rec.bodies("/api/chat")) == 2


async def test_structured_rejects_extra_valid_json_of_wrong_shape() -> None:
    rec = Recorder({"/api/chat": [_chat('{"unrelated": 1}')]})
    with pytest.raises(ModelSchemaViolationError) as info:
        await _provider(rec).complete_structured(
            role=ModelRole.FAST, messages=MSGS, schema=Verdict, max_attempts=1
        )
    assert info.value.raw_text == '{"unrelated": 1}'


async def test_structured_invalid_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        await _provider(Recorder({})).complete_structured(
            role=ModelRole.FAST, messages=MSGS, schema=Verdict, max_attempts=0
        )


# -------------------------------------------------------------------- embed
async def test_embed_missing_model_is_unavailable_without_fallback() -> None:
    rec = Recorder({"/api/tags": [_tags((FAST, ["completion", "tools", "thinking"]))]})
    with pytest.raises(ModelUnavailableError, match="not installed"):
        await _provider(rec).embed(texts=["hello"])
    assert rec.bodies("/api/embed") == []
    assert all(r.url.path != "/api/chat" for r in rec.requests)


async def test_embed_refuses_completion_model() -> None:
    rec = Recorder({"/api/tags": [_tags((FAST, ["completion"]))]})
    with pytest.raises(ModelUnavailableError, match="embedding"):
        await _provider(rec, model_embedding=FAST).embed(texts=["hello"])
    assert rec.bodies("/api/embed") == []


async def test_embed_success_and_latest_equivalence() -> None:
    rec = Recorder(
        {
            "/api/tags": [_tags((f"{EMBED}:latest", ["embedding"]))],
            "/api/embed": [
                httpx.Response(
                    200, json={"model": EMBED, "embeddings": [[0.1, 0.2, 0.3], [0.3, 0.2, 0.1]]}
                )
            ],
        }
    )
    provider = _provider(rec)
    result = await provider.embed(texts=["a", "b"])
    assert result.dimensions == 3
    assert len(result.vectors) == 2
    assert result.model == EMBED
    # One request for the whole batch; truncate is explicit so long pages never error.
    assert rec.bodies("/api/embed") == [{"model": EMBED, "input": ["a", "b"], "truncate": True}]


async def test_embed_404_maps_to_unavailable() -> None:
    rec = Recorder(
        {
            "/api/tags": [_tags((EMBED, None))],
            "/api/embed": [httpx.Response(404, json={"error": "model not found"})],
        }
    )
    with pytest.raises(ModelUnavailableError, match="404"):
        await _provider(rec).embed(texts=["a"])


def _embed_routes(embed: Handler | list[httpx.Response]) -> Recorder:
    return Recorder({"/api/tags": lambda _r: _tags((EMBED, ["embedding"])), "/api/embed": embed})


async def test_embed_timeout_maps_to_model_timeout_error() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ModelTimeoutError, match="/api/embed"):
        await _provider(_embed_routes(slow)).embed(texts=["a"])


async def test_embed_unreachable_maps_to_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(ModelUnavailableError, match="unreachable"):
        await _provider(_embed_routes(refuse)).embed(texts=["a"])
    # Unreachable during verification too.
    with pytest.raises(ModelUnavailableError, match="unreachable"):
        await _provider(Recorder({"/api/tags": refuse})).embed(texts=["a"])


@pytest.mark.parametrize(
    ("embeddings", "texts"),
    [
        ([[0.1, 0.2]], ["a", "b"]),  # fewer vectors than inputs
        ([[0.1, 0.2], [0.1, 0.2], [0.3, 0.4]], ["a", "b"]),  # more vectors than inputs
        ([[0.1, 0.2], [0.1]], ["a", "b"]),  # ragged dimensions
        ([[]], ["a"]),  # empty vector
        ([], ["a"]),  # no vectors at all
    ],
)
async def test_embed_inconsistent_vectors_are_provider_errors(
    embeddings: list[list[float]], texts: list[str]
) -> None:
    rec = _embed_routes([httpx.Response(200, json={"model": EMBED, "embeddings": embeddings})])
    with pytest.raises(ModelProviderError) as info:
        await _provider(rec).embed(texts=texts)
    # A malformed answer is NOT "unavailable": retrying elsewhere will not help.
    assert not isinstance(info.value, ModelUnavailableError | ModelTimeoutError)


async def test_embed_malformed_body_is_provider_error() -> None:
    rec = _embed_routes([httpx.Response(200, json={"model": EMBED, "vectors": [[1.0]]})])
    with pytest.raises(ModelProviderError, match="Malformed"):
        await _provider(rec).embed(texts=["a"])


async def test_embed_reply_for_a_different_model_is_provider_error() -> None:
    rec = _embed_routes([httpx.Response(200, json={"model": "other:7b", "embeddings": [[1.0]]})])
    with pytest.raises(ModelProviderError, match="other:7b"):
        await _provider(rec).embed(texts=["a"])


async def test_embed_never_logs_input_text() -> None:
    secret = "CONFIDENTIAL page body about acquisitions"
    rec = _embed_routes([httpx.Response(200, json={"model": EMBED, "embeddings": [[0.6, 0.8]]})])
    with structlog.testing.capture_logs() as logs:
        await _provider(rec).embed(texts=[secret])
    [event] = [e for e in logs if e["event"] == "model.embed"]
    assert event["count"] == 1
    assert event["dimensions"] == 2
    assert secret not in repr(logs)


async def test_embed_empty_input_is_rejected_without_io() -> None:
    rec = _embed_routes([])
    with pytest.raises(ValueError, match="empty"):
        await _provider(rec).embed(texts=[])
    assert rec.requests == []


# ------------------------------------------------------------- capabilities
async def test_capabilities_role_statuses_with_latest_equivalence() -> None:
    rec = Recorder(
        {
            "/api/tags": [
                _tags((FAST, ["completion", "tools", "thinking"]), ("other:latest", None))
            ],
            "/api/version": [httpx.Response(200, json={"version": "0.35.0"})],
        }
    )
    caps = await _provider(rec, model_reasoning="other").capabilities()
    statuses = {s.role: (s.model, s.status) for s in caps.roles}
    assert statuses[ModelRole.FAST] == (FAST, "ok")
    assert statuses[ModelRole.REASONING] == ("other", "ok")
    assert statuses[ModelRole.EMBEDDING] == (EMBED, "missing")
    assert caps.supports_embeddings is False
    assert caps.embedding_dimensions is None
    assert caps.supports_tools is True
    assert caps.supports_json_schema is True
    assert caps.models_available == [FAST, "other:latest"]
    assert caps.provider == "ollama"


async def test_capabilities_embedding_ok_but_dimension_unknown_until_observed() -> None:
    rec = Recorder(
        {
            "/api/tags": lambda _r: _tags((EMBED, ["embedding"]), (FAST, ["completion"])),
            "/api/version": lambda _r: httpx.Response(200, json={"version": "0.4.7"}),
            "/api/embed": [httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})],
        }
    )
    provider = _provider(rec)
    caps = await provider.capabilities()
    assert caps.supports_embeddings is True
    assert caps.embedding_dimensions is None
    assert caps.supports_tools is False
    assert caps.supports_json_schema is False  # older than 0.5.0
    statuses = {s.role: s.status for s in caps.roles}
    assert statuses[ModelRole.REASONING] == "missing"
    await provider.embed(texts=["x"])
    assert (await provider.capabilities()).embedding_dimensions == 2


# ------------------------------------------------------------------- health
async def test_health_reachable() -> None:
    rec = Recorder({"/api/tags": [_tags((FAST, None))]})
    health = await _provider(rec).health()
    assert health.reachable is True
    assert health.latency_ms is not None
    assert health.detail == {"models_installed": 1}
    assert health.error is None


async def test_health_never_raises_on_connection_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    provider = _provider(
        Recorder({"/api/tags": refuse}),
        ollama_base_url="http://user:hunter2@ollama.test:11434",
    )
    health = await provider.health()
    assert health.reachable is False
    assert health.error is not None
    assert "ConnectError" in health.error
    assert "hunter2" not in health.model_dump_json()


async def test_health_reports_http_status_and_timeout() -> None:
    rec = Recorder({"/api/tags": [httpx.Response(500, json={"error": "boom"})]})
    health = await _provider(rec).health()
    assert health.reachable is False
    assert health.error == "HTTP 500 from /api/tags"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    health = await _provider(Recorder({"/api/tags": slow})).health()
    assert health.reachable is False
    assert health.error is not None
    assert health.error.startswith("timeout")


# ------------------------------------------------------------ error mapping
async def test_timeout_maps_to_model_timeout_error() -> None:
    seen: list[dict[str, Any]] = []

    def slow(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        raise httpx.ReadTimeout("timed out", request=request)

    provider = _provider(Recorder({"/api/chat": slow}))
    with pytest.raises(ModelTimeoutError):
        await provider.complete(
            role=ModelRole.FAST, messages=MSGS, options=GenerationOptions(timeout_s=1.5)
        )
    assert seen[0]["read"] == 1.5


async def test_connect_error_maps_to_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(ModelUnavailableError, match="unreachable"):
        await _provider(Recorder({"/api/chat": refuse})).complete(
            role=ModelRole.FAST, messages=MSGS
        )


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_non_2xx_maps_to_unavailable_with_status(status: int) -> None:
    rec = Recorder({"/api/chat": [httpx.Response(status, json={"error": "nope"})]})
    with pytest.raises(ModelUnavailableError, match=f"HTTP {status}"):
        await _provider(rec).complete_structured(role=ModelRole.FAST, messages=MSGS, schema=Verdict)


async def test_error_messages_do_not_leak_base_url_credentials() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    provider = _provider(
        Recorder({"/api/chat": refuse}), ollama_base_url="http://u:hunter2@ollama.test:1"
    )
    with pytest.raises(ModelUnavailableError) as info:
        await provider.complete(role=ModelRole.FAST, messages=MSGS)
    assert "hunter2" not in str(info.value)


# ------------------------------------------------------------------ lifecycle
async def test_aclose_is_idempotent_and_does_not_close_injected_client() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(Recorder({})))
    provider = OllamaProvider(_settings(), client=client)
    await provider.aclose()
    await provider.aclose()
    assert client.is_closed is False
    with pytest.raises(ModelUnavailableError, match="closed"):
        await provider.complete(role=ModelRole.FAST, messages=MSGS)
    assert (await provider.health()).reachable is False
    await client.aclose()


async def test_aclose_closes_owned_client() -> None:
    provider = OllamaProvider(_settings())
    await provider.aclose()
    await provider.aclose()
    assert provider._client.is_closed is True


async def test_default_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: prompts must never be routed through an HTTP(S)/system proxy.

    httpx honours HTTP(S)_PROXY / ALL_PROXY and macOS proxy settings when
    trust_env is true; page content would then leave the machine.
    """
    from ken_core.config import Settings
    from ken_core.providers.model.ollama import OllamaProvider

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid:3128")
    provider = OllamaProvider(Settings(_env_file=None, model_provider="ollama"))
    try:
        assert provider._client.trust_env is False
    finally:
        await provider.aclose()

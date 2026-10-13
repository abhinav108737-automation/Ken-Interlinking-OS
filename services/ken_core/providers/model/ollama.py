"""OllamaProvider: the Phase 0 ModelProvider backed by a local Ollama daemon.

Rules this module enforces:

* Models are resolved by ROLE via :meth:`Settings.role_model`; no model name is
  hardcoded here (CLAUDE.md, Model Design Rule).
* ``complete_structured`` returns a value only after it has passed
  ``schema.model_validate_json``. Raw output never escapes as a validated value
  (TRD.md section 6); failure is a :class:`ModelSchemaViolationError` carrying
  the raw text so it can become an evaluation fixture.
* ``embed`` never falls back to a completion model. A completion model's hidden
  size is not an embedding dimension.
* ``health`` never raises, and neither errors nor logs carry the base URL
  (which could embed credentials) or prompt content at INFO.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ken_contracts.model import (
    Completion,
    EmbeddingResult,
    GenerationOptions,
    Message,
    ModelProviderError,
    ModelRole,
    ModelRoleStatus,
    ModelSchemaViolationError,
    ModelTimeoutError,
    ModelUnavailableError,
    ProviderCapabilities,
    ProviderHealth,
    StructuredResult,
    TokenUsage,
)
from ken_core.config import Settings
from ken_core.logging import get_logger
from ken_core.providers.model._text import clean_structured_output, strip_think

PROVIDER_NAME = "ollama"

# Ollama added JSON-schema values for the ``format`` field in 0.5.0.
_JSON_SCHEMA_MIN_VERSION = (0, 5, 0)
_METADATA_TIMEOUT_S = 10.0
_EMBED_TIMEOUT_S = 60.0
_MAX_ERROR_FEEDBACK_CHARS = 1000
_MAX_ECHOED_OUTPUT_CHARS = 4000

_REPAIR_INSTRUCTION = (
    "Your previous reply did not validate against the required JSON schema. "
    "Validation errors: {errors}\n"
    "Reply again with only a single JSON object that matches the schema exactly."
)

_log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Wire models for Ollama responses. Lenient on extra keys (Ollama adds fields
# across versions) but strict on the fields we actually consume.
# --------------------------------------------------------------------------- #
class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ChatMessage(_Wire):
    role: str = "assistant"
    content: str = ""


class _ChatResponse(_Wire):
    model: str | None = None
    message: _ChatMessage
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class _EmbedResponse(_Wire):
    model: str | None = None
    embeddings: list[list[float]]


class _TagModel(_Wire):
    name: str
    model: str | None = None
    capabilities: list[str] | None = None


class _TagsResponse(_Wire):
    models: list[_TagModel] = Field(default_factory=list)


class _VersionResponse(_Wire):
    version: str


class _HTTPStatusError(ModelUnavailableError):
    """Non-2xx answer from Ollama. Private: callers see ModelUnavailableError."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _normalise_model_name(name: str) -> str:
    """Ollama treats ``foo`` and ``foo:latest`` as the same model."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _parse_version(text: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for piece in text.split("-", 1)[0].split(".")[:3]:
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def _elapsed_ms(start: float) -> float:
    return max(0.0, (time.perf_counter() - start) * 1000.0)


def _summarise_validation_error(exc: ValidationError) -> str:
    """Compact error summary for repair feedback. Never echoes input values."""
    items = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "; ".join(items)[:_MAX_ERROR_FEEDBACK_CHARS]


_ROLE_CAPABILITY: dict[ModelRole, str] = {
    ModelRole.FAST: "completion",
    ModelRole.REASONING: "completion",
    ModelRole.EMBEDDING: "embedding",
}


class OllamaProvider:
    """ModelProvider over Ollama's HTTP API (``/api/chat``, ``/api/embed``, ``/api/tags``).

    ``client`` may be injected (tests use ``httpx.MockTransport``). An injected
    client is owned by the caller and is NOT closed by :meth:`aclose`; a client
    the provider creates itself is.
    """

    name: str = PROVIDER_NAME

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._owns_client = client is None
        # trust_env=False: never route through HTTP(S)_PROXY / ALL_PROXY or the
        # macOS system proxy. Prompts carry page content (from Phase 1) and must
        # reach the LOCAL model server directly, never via an external proxy.
        self._client = client if client is not None else httpx.AsyncClient(trust_env=False)
        self._closed = False
        self._verified_embedding_models: set[str] = set()
        self._embedding_dims: dict[str, int] = {}

    # ----------------------------------------------------------- transport
    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        timeout_s: float,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request and map transport failures onto typed errors."""
        if self._closed:
            raise ModelUnavailableError("OllamaProvider is closed")
        try:
            response = await self._client.request(
                method, f"{self._base_url}{path}", json=json, timeout=httpx.Timeout(timeout_s)
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"Ollama did not answer {path} within {timeout_s:g}s") from exc
        except httpx.TransportError as exc:
            raise ModelUnavailableError(
                f"Ollama is unreachable ({type(exc).__name__}) for {path}"
            ) from exc
        if not response.is_success:
            raise _HTTPStatusError(
                f"Ollama returned HTTP {response.status_code} for {path}: "
                f"{self._error_text(response)}",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return str(body["error"])[:200]
        return response.text[:200]

    @staticmethod
    def _parse[W: _Wire](response: httpx.Response, wire: type[W], path: str) -> W:
        try:
            return wire.model_validate_json(response.content)
        except ValidationError as exc:
            raise ModelProviderError(f"Malformed Ollama response from {path}") from exc

    async def _tags(self, *, timeout_s: float = _METADATA_TIMEOUT_S) -> _TagsResponse:
        response = await self._request("GET", "/api/tags", timeout_s=timeout_s)
        return self._parse(response, _TagsResponse, "/api/tags")

    # ------------------------------------------------------------- completion
    def _chat_body(
        self,
        model: str,
        messages: Sequence[Message],
        options: GenerationOptions,
        fmt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        model_options: dict[str, Any] = {"temperature": options.temperature}
        if options.top_p is not None:
            model_options["top_p"] = options.top_p
        if options.max_tokens is not None:
            model_options["num_predict"] = options.max_tokens
        if options.seed is not None:
            model_options["seed"] = options.seed
        if options.stop:
            model_options["stop"] = list(options.stop)
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "think": options.think,
            "options": model_options,
        }
        if fmt is not None:
            body["format"] = fmt
        return body

    async def _chat(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions,
        fmt: dict[str, Any] | None,
    ) -> tuple[_ChatResponse, str, float]:
        model = self._settings.role_model(role)
        start = time.perf_counter()
        response = await self._request(
            "POST",
            "/api/chat",
            timeout_s=options.timeout_s,
            json=self._chat_body(model, messages, options, fmt),
        )
        parsed = self._parse(response, _ChatResponse, "/api/chat")
        return parsed, model, _elapsed_ms(start)

    @staticmethod
    def _completion(
        parsed: _ChatResponse, *, text: str, model: str, role: ModelRole, latency_ms: float
    ) -> Completion:
        usage = None
        if parsed.prompt_eval_count is not None or parsed.eval_count is not None:
            usage = TokenUsage(
                prompt_tokens=parsed.prompt_eval_count, completion_tokens=parsed.eval_count
            )
        return Completion(
            text=text,
            model=parsed.model or model,
            role=role,
            finish_reason=parsed.done_reason,
            usage=usage,
            latency_ms=latency_ms,
            provider=PROVIDER_NAME,
        )

    async def complete(
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        options: GenerationOptions | None = None,
    ) -> Completion:
        opts = options or GenerationOptions()
        parsed, model, latency_ms = await self._chat(
            role=role, messages=messages, options=opts, fmt=None
        )
        text = strip_think(parsed.message.content)
        _log.info(
            "model.complete",
            provider=PROVIDER_NAME,
            role=role.value,
            model=model,
            latency_ms=round(latency_ms, 1),
            finish_reason=parsed.done_reason,
        )
        return self._completion(parsed, text=text, model=model, role=role, latency_ms=latency_ms)

    async def complete_structured[S: BaseModel](
        self,
        *,
        role: ModelRole,
        messages: Sequence[Message],
        schema: type[S],
        options: GenerationOptions | None = None,
        max_attempts: int = 2,
    ) -> StructuredResult[S]:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        opts = options or GenerationOptions()
        json_schema = schema.model_json_schema()
        conversation = list(messages)
        last_raw = ""
        total_latency_ms = 0.0
        for attempt in range(1, max_attempts + 1):
            parsed, model, latency_ms = await self._chat(
                role=role, messages=conversation, options=opts, fmt=json_schema
            )
            total_latency_ms += latency_ms
            last_raw = parsed.message.content
            candidate, repaired = clean_structured_output(last_raw)
            try:
                value = schema.model_validate_json(candidate)
            except ValidationError as exc:
                feedback = _summarise_validation_error(exc)
                _log.warning(
                    "model.structured.invalid",
                    provider=PROVIDER_NAME,
                    role=role.value,
                    model=model,
                    schema=schema.__name__,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    latency_ms=round(latency_ms, 1),
                    raw_chars=len(last_raw),
                )
                _log.debug("model.structured.invalid_detail", errors=feedback)
                conversation = [
                    *conversation,
                    Message(role="assistant", content=candidate[:_MAX_ECHOED_OUTPUT_CHARS]),
                    Message(role="user", content=_REPAIR_INSTRUCTION.format(errors=feedback)),
                ]
                continue
            _log.info(
                "model.complete_structured",
                provider=PROVIDER_NAME,
                role=role.value,
                model=model,
                schema=schema.__name__,
                attempts=attempt,
                repaired=repaired,
                latency_ms=round(total_latency_ms, 1),
            )
            completion = self._completion(
                parsed, text=candidate, model=model, role=role, latency_ms=total_latency_ms
            )
            return StructuredResult(
                value=value, completion=completion, attempts=attempt, repaired=repaired
            )
        raise ModelSchemaViolationError(
            f"Output did not validate against {schema.__name__} after {max_attempts} attempt(s)",
            raw_text=last_raw,
            attempts=max_attempts,
        )

    # -------------------------------------------------------------- embedding
    async def _verify_embedding_model(self, role: ModelRole, model: str) -> None:
        """Refuse to embed with a model that is absent or is not an embedding model."""
        if model in self._verified_embedding_models:
            return
        tags = await self._tags()
        wanted = _normalise_model_name(model)
        match = next((m for m in tags.models if _normalise_model_name(m.name) == wanted), None)
        if match is None:
            raise ModelUnavailableError(
                f"Embedding model {model!r} (role={role.value}) is not installed on the Ollama "
                "server. No fallback to a completion model is attempted; install an embedding "
                "model and set KEN_MODEL_EMBEDDING."
            )
        if match.capabilities is not None and "embedding" not in match.capabilities:
            raise ModelUnavailableError(
                f"Model {model!r} (role={role.value}) does not advertise the 'embedding' "
                "capability; a completion model's hidden states are not embeddings."
            )
        self._verified_embedding_models.add(model)

    async def embed(
        self,
        *,
        texts: Sequence[str],
        role: ModelRole = ModelRole.EMBEDDING,
    ) -> EmbeddingResult:
        if not texts:
            raise ValueError("texts must not be empty")
        model = self._settings.role_model(role)
        await self._verify_embedding_model(role, model)
        start = time.perf_counter()
        try:
            response = await self._request(
                "POST",
                "/api/embed",
                timeout_s=_EMBED_TIMEOUT_S,
                json={"model": model, "input": list(texts)},
            )
        except ModelUnavailableError:
            # The model may have been removed since verification.
            self._verified_embedding_models.discard(model)
            raise
        parsed = self._parse(response, _EmbedResponse, "/api/embed")
        latency_ms = _elapsed_ms(start)
        vectors = parsed.embeddings
        dims = {len(v) for v in vectors}
        if len(vectors) != len(texts) or len(dims) != 1 or 0 in dims:
            raise ModelProviderError(
                f"Ollama returned {len(vectors)} embeddings of inconsistent or empty "
                f"dimensions for {len(texts)} input(s)"
            )
        dimensions = dims.pop()
        self._embedding_dims[model] = dimensions
        _log.info(
            "model.embed",
            provider=PROVIDER_NAME,
            role=role.value,
            model=model,
            count=len(texts),
            dimensions=dimensions,
            latency_ms=round(latency_ms, 1),
        )
        return EmbeddingResult(
            vectors=vectors,
            model=parsed.model or model,
            dimensions=dimensions,
            latency_ms=latency_ms,
            provider=PROVIDER_NAME,
        )

    # ------------------------------------------------------- introspection
    async def _supports_json_schema(self) -> bool:
        try:
            response = await self._request("GET", "/api/version", timeout_s=_METADATA_TIMEOUT_S)
            version = _parse_version(
                self._parse(response, _VersionResponse, "/api/version").version
            )
        except ModelProviderError:
            return False
        return version is not None and version >= _JSON_SCHEMA_MIN_VERSION

    async def capabilities(self) -> ProviderCapabilities:
        tags = await self._tags()
        installed = {_normalise_model_name(m.name): m for m in tags.models}
        statuses: list[ModelRoleStatus] = []
        role_ok: dict[ModelRole, bool] = {}
        for role in ModelRole:
            model = self._settings.role_model(role)
            match = installed.get(_normalise_model_name(model))
            ok = match is not None and (
                match.capabilities is None or _ROLE_CAPABILITY[role] in match.capabilities
            )
            role_ok[role] = ok
            statuses.append(
                ModelRoleStatus(role=role, model=model, status="ok" if ok else "missing")
            )

        completion_models = [
            installed[_normalise_model_name(self._settings.role_model(r))]
            for r in (ModelRole.FAST, ModelRole.REASONING)
            if role_ok[r]
        ]
        supports_tools = any(
            m.capabilities is not None and "tools" in m.capabilities for m in completion_models
        )
        supports_embeddings = role_ok[ModelRole.EMBEDDING]
        embedding_dimensions = (
            self._embedding_dims.get(self._settings.role_model(ModelRole.EMBEDDING))
            if supports_embeddings
            else None
        )
        return ProviderCapabilities(
            provider=PROVIDER_NAME,
            supports_tools=supports_tools,
            supports_json_schema=await self._supports_json_schema(),
            supports_embeddings=supports_embeddings,
            embedding_dimensions=embedding_dimensions,
            models_available=sorted(m.name for m in tags.models),
            roles=statuses,
        )

    async def health(self) -> ProviderHealth:
        start = time.perf_counter()
        timeout_s = self._settings.readiness_timeout_ms / 1000.0
        try:
            tags = await self._tags(timeout_s=timeout_s)
        except ModelTimeoutError:
            error = f"timeout after {timeout_s:g}s"
        except _HTTPStatusError as exc:
            error = f"HTTP {exc.status_code} from /api/tags"
        except ModelUnavailableError as exc:
            cause = exc.__cause__
            error = (
                f"unreachable ({type(cause).__name__})"
                if isinstance(cause, httpx.TransportError)
                else "unavailable (provider closed)"
                if self._closed
                else "unavailable"
            )
        except Exception as exc:  # health must never raise
            error = f"error ({type(exc).__name__})"
        else:
            return ProviderHealth(
                provider=PROVIDER_NAME,
                reachable=True,
                latency_ms=_elapsed_ms(start),
                detail={"models_installed": len(tags.models)},
            )
        _log.warning("model.health.unreachable", provider=PROVIDER_NAME, error=error)
        return ProviderHealth(
            provider=PROVIDER_NAME, reachable=False, latency_ms=_elapsed_ms(start), error=error
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

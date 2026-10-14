"""No data leaves the machine (ADR-0012 section 9).

Importing ``langgraph.graph`` loads ``langsmith``, ``langgraph_sdk``, ``requests``,
``httpx2`` and ``websockets``. LangSmith tracing would ship run inputs (page text, plans)
to a remote service, so an agent run REFUSES to start when tracing is requested or a
LangSmith/LangChain endpoint or key is configured, and tracing is forced off before
``langgraph`` is first imported (``langsmith`` caches its environment lookups).

Call :func:`enforce_no_remote_tracing` before importing ``langgraph`` (the graph builder
imports it lazily, after this call).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from typing import Final

__all__ = [
    "FORBIDDEN_SETTINGS",
    "FORCED_OFF",
    "NAMESPACES",
    "TRACING_FLAGS",
    "RemoteTracingConfiguredError",
    "enforce_no_remote_tracing",
    "remote_tracing_problems",
]

NAMESPACES: Final = ("LANGSMITH_", "LANGCHAIN_")
"""LangSmith resolves EVERY setting through ``get_env_var(name)``, which tries
``LANGSMITH_<name>`` then ``LANGCHAIN_<name>`` -- so a name-by-name list always misses one
(QA C1: ``LANGSMITH_TRACING_V2``). The rule is therefore fail-closed: ANY variable in
either namespace whose value is not falsy refuses the run."""
TRACING_FLAGS: Final = tuple(
    f"{namespace}{flag}"
    for namespace in NAMESPACES
    for flag in ("TRACING", "TRACING_V2", "OTEL_ENABLED", "OTEL_ONLY")
)
"""Forced to ``"false"`` once the refusal check has passed."""
FORBIDDEN_SETTINGS: Final = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
    "LANGSMITH_RUNS_ENDPOINTS",
    "LANGCHAIN_RUNS_ENDPOINTS",
)
"""Named explicitly in the refusal message (covered by the namespace rule too)."""
FORCED_OFF: Final = TRACING_FLAGS
_FALSY: Final = frozenset({"", "0", "false", "no", "off", "none", "null"})


class RemoteTracingConfiguredError(RuntimeError):
    """Remote tracing is requested or configured; the agent run refuses to start.

    The message names variables only, never their values.
    """

    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__(
            "remote tracing must be off for agent runs (ADR-0012 section 9); unset: "
            + ", ".join(names)
        )
        self.names = names


def _truthy(value: str) -> bool:
    return value.strip().lower() not in _FALSY


def remote_tracing_problems(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Names of the variables that make ``environ`` unsafe, sorted: every ``LANGSMITH_*`` /
    ``LANGCHAIN_*`` variable whose value is not falsy."""
    return tuple(
        sorted(
            name
            for name, value in environ.items()
            if name.upper().startswith(NAMESPACES) and _truthy(value)
        )
    )


def enforce_no_remote_tracing(environ: MutableMapping[str, str] | None = None) -> None:
    """Refuse remote tracing, then force every tracing flag to ``"false"``.

    Raises:
        RemoteTracingConfiguredError: a tracing flag is truthy or a key/endpoint is set.
    """
    env = os.environ if environ is None else environ
    problems = remote_tracing_problems(env)
    if problems:
        raise RemoteTracingConfiguredError(problems)
    for name in FORCED_OFF:
        env[name] = "false"  # overwrite: every value here is already falsy
    if environ is None:
        _clear_langsmith_cache()


def _clear_langsmith_cache() -> None:
    """``langsmith.utils.get_env_var`` is ``lru_cache``d: if langsmith was imported earlier
    in this process, drop its cached answers so the forced values are the ones it reads."""
    import sys

    utils = sys.modules.get("langsmith.utils")
    getter = getattr(utils, "get_env_var", None) if utils is not None else None
    clear = getattr(getter, "cache_clear", None)
    if callable(clear):
        clear()

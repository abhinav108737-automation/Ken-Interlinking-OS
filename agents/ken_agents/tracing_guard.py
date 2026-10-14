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
    "TRACING_FLAGS",
    "RemoteTracingConfiguredError",
    "enforce_no_remote_tracing",
    "remote_tracing_problems",
]

TRACING_FLAGS: Final = (
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGSMITH_OTEL_ENABLED",
)
"""Refused when truthy."""
FORBIDDEN_SETTINGS: Final = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
)
"""Refused when set to anything non-empty (the value is never read into a message)."""
FORCED_OFF: Final = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
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
    """Names of the variables that make ``environ`` unsafe, in a fixed order."""
    flags = [name for name in TRACING_FLAGS if _truthy(environ.get(name, ""))]
    settings = [name for name in FORBIDDEN_SETTINGS if environ.get(name, "").strip()]
    return (*flags, *settings)


def enforce_no_remote_tracing(environ: MutableMapping[str, str] | None = None) -> None:
    """Refuse remote tracing, then force the tracing flags off (``setdefault "false"``).

    Raises:
        RemoteTracingConfiguredError: a tracing flag is truthy or a key/endpoint is set.
    """
    env = os.environ if environ is None else environ
    problems = remote_tracing_problems(env)
    if problems:
        raise RemoteTracingConfiguredError(problems)
    for name in FORCED_OFF:
        env.setdefault(name, "false")

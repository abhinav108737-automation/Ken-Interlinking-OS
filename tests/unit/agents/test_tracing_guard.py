"""ADR-0012 section 9: no remote tracing; tracing forced off before langgraph is imported."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from ken_agents.graph import StepRecorder, build_graph
from ken_agents.tracing_guard import (
    FORBIDDEN_SETTINGS,
    TRACING_FLAGS,
    RemoteTracingConfiguredError,
    enforce_no_remote_tracing,
    remote_tracing_problems,
)
from tests.unit.agents._support import FakeBackend, FakeStore, make_plan, make_policy, run_source


@pytest.mark.parametrize("name", TRACING_FLAGS)
@pytest.mark.parametrize("value", ["true", "1", "TRUE", "yes", "on"])
def test_truthy_tracing_flag_is_refused(name: str, value: str) -> None:
    env = {name: value}
    with pytest.raises(RemoteTracingConfiguredError) as caught:
        enforce_no_remote_tracing(env)
    assert caught.value.names == (name,)


@pytest.mark.parametrize("value", ["", "false", "0", "False", "off", "no"])
def test_falsy_tracing_flags_are_allowed(value: str) -> None:
    env = dict.fromkeys(TRACING_FLAGS, value)
    enforce_no_remote_tracing(env)


@pytest.mark.parametrize("name", FORBIDDEN_SETTINGS)
def test_any_key_or_endpoint_is_refused_and_its_value_never_shown(name: str) -> None:
    env = {name: "lsv2_super_secret_value"}
    with pytest.raises(RemoteTracingConfiguredError) as caught:
        enforce_no_remote_tracing(env)
    assert name in str(caught.value)
    assert "super_secret" not in str(caught.value)


def test_blank_settings_are_allowed() -> None:
    assert remote_tracing_problems(dict.fromkeys(FORBIDDEN_SETTINGS, "  ")) == ()


def test_tracing_is_forced_off_with_setdefault() -> None:
    env: dict[str, str] = {"LANGCHAIN_TRACING_V2": "0"}
    enforce_no_remote_tracing(env)
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING_V2"] == "0", "an explicit falsy value is kept"


def test_build_graph_refuses_when_tracing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_x")

    async def sink(_: object) -> None:
        return None

    with pytest.raises(RemoteTracingConfiguredError):
        build_graph(
            ctx=None,  # type: ignore[arg-type]
            recorder=StepRecorder(run_id=uuid.uuid4(), trace_id="0" * 32, sink=sink, clock=None),  # type: ignore[arg-type]
        )


async def test_runner_refuses_before_starting_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    store = FakeStore()
    with pytest.raises(RemoteTracingConfiguredError):
        await run_source(FakeBackend.ready([make_plan(0)]), store, None, make_policy())
    assert store.runs == {}


def test_langgraph_is_imported_lazily_and_tracing_is_off_in_a_fresh_process() -> None:
    script = textwrap.dedent(
        """
        import sys
        import ken_agents.graph, ken_agents.runner, ken_agents.nodes, ken_agents.tools
        assert "langgraph" not in sys.modules, "langgraph imported eagerly"
        assert "langsmith" not in sys.modules, "langsmith imported eagerly"
        from ken_agents.tracing_guard import enforce_no_remote_tracing
        enforce_no_remote_tracing()
        import langgraph.graph  # what build_graph does next
        from langsmith import utils
        assert not utils.tracing_is_enabled(), "tracing must be off"
        print("ok")
        """
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in TRACING_FLAGS and k not in FORBIDDEN_SETTINGS
    }
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter, a literal script
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "ok"

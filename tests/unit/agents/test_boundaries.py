"""Static and runtime boundaries of the agent runtime (ADR-0012 sections 6, 7 and 9)."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import socket
import sys
from pathlib import Path

import pytest

from ken_agents.nodes import NodeContext
from ken_agents.tools import ToolBox
from ken_contracts.agents import AgentOutcome, CriticAssessment, ToolName
from ken_core.providers.model.mock import MockModelProvider
from tests.unit.agents._support import FakeBackend, FakeStore, make_plan, make_policy, run_source

PACKAGE = Path(__file__).resolve().parents[3] / "agents" / "ken_agents"
WORKER = "worker.py"
"""The Phase 0 heartbeat (ADR-0005) is not an agent: it owns its DB session by design and
has its own boundary test (tests/unit/test_worker.py)."""
COMPOSITION_ROOT = "cli.py"
"""The only module that wires the database-backed services (lazily, inside ``_run``)."""

FORBIDDEN_EVERYWHERE = (
    "sqlalchemy",
    "httpx",
    "httpx2",
    "requests",
    "subprocess",
    "socket",
    "langchain",
    "langsmith",
    "ken_core.providers.model.ollama",
    "ken_core.providers.cms",
    "ken_contracts.cms",
    # Dynamic import, native code, processes, shells and file trees (QA M1).
    "importlib",
    "ctypes",
    "pty",
    "shutil",
    "multiprocessing",
    "signal",
    "pickle",
)
OS_ALLOWED_IN = frozenset({"tracing_guard.py"})
"""``os`` (only ``os.environ``) is needed by the tracing guard and nowhere else."""
FORBIDDEN_CALLS = frozenset({"__import__", "open", "exec", "eval", "compile", "breakpoint"})
FORBIDDEN_ATTRIBUTE_PREFIXES = (
    "system",
    "popen",
    "exec",
    "spawn",
    "posix_spawn",
    "fork",
    "create_subprocess",
    "startfile",
)
"""Attribute names refused on ANY object, whatever the alias (``import os as o; o.system``)."""
FORBIDDEN_OUTSIDE_ROOT = (
    "ken_core.db",
    "ken_core.providers",
    "ken_services.agents.backend",
    "ken_services.agents.store",
    "ken_services.jobs",
)


def _sources() -> list[Path]:
    files = sorted(p for p in PACKAGE.glob("*.py") if p.name != WORKER)
    assert {p.name for p in files} >= {"graph.py", "nodes.py", "tools.py", "runner.py", "cli.py"}
    return files


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _hits(imported: set[str], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(m for m in imported if any(m == p or m.startswith(p + ".") for p in prefixes))


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_infrastructure_imports(path: Path) -> None:
    imported = _imports(path)
    assert _hits(imported, FORBIDDEN_EVERYWHERE) == []
    if path.name != COMPOSITION_ROOT:
        assert _hits(imported, FORBIDDEN_OUTSIDE_ROOT) == []


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_os_module_outside_the_tracing_guard(path: Path) -> None:
    if path.name not in OS_ALLOWED_IN:
        assert _hits(_imports(path), ("os", "posix", "nt")) == []


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_shell_no_human_actor_no_approval(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert not node.attr.startswith(FORBIDDEN_ATTRIBUTE_PREFIXES), (path.name, node.attr)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_ATTRIBUTE_PREFIXES), alias.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_CALLS, (path.name, node.func.id)
        if isinstance(node, ast.Name):
            assert node.id not in {"__import__", "__builtins__"}, (path.name, node.id)
    assert "user:" not in text, "agents never act as a human actor"
    assert "LinkPlanStatus.APPROVED" not in text and '"approved"' not in text
    assert "AUTO_APPROVE = True" not in text


def test_langgraph_is_imported_only_inside_the_graph_builder() -> None:
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert not any(n.startswith("langgraph") for n in names), path.name


def test_nodes_get_only_tools_policy_and_role_models() -> None:
    assert [f.name for f in dataclasses.fields(NodeContext)] == [
        "tools",
        "policy",
        "planner",
        "critic",
    ]


def test_toolbox_exposes_exactly_the_allowlist() -> None:
    public = {
        name
        for name, member in inspect.getmembers(ToolBox, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {t.value for t in ToolName} | {"bind"}


async def test_full_graph_runs_with_outbound_sockets_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("outbound network blocked by test")

    def refuse_create(*args: object, **kwargs: object) -> socket.socket:
        attempts.append(args)
        raise OSError("outbound network blocked by test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse_create)

    provider = MockModelProvider(dimensions=8)
    provider.register(
        CriticAssessment,
        {"critiques": [{"ref": "c0", "verdict": "accept"}, {"ref": "c1", "verdict": "uncertain"}]},
    )
    result = await run_source(
        FakeBackend.ready([make_plan(0), make_plan(1)]),
        FakeStore(),
        provider,
        make_policy(planner=False, critic=True),
    )
    assert result.outcome is AgentOutcome.RECOMMEND
    assert attempts == [], "nothing tried to leave the machine"


# --------------------------------------------------------------------------- #
# Runtime: a process-wide audit hook watches a full graph run (QA M1)
# --------------------------------------------------------------------------- #
_AUDIT: dict[str, object] = {"on": False, "events": []}
_WATCHED = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "socket.connect",
    "socket.getaddrinfo",
    "ctypes.dlopen",
)


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _AUDIT["on"]:
        return
    events = _AUDIT["events"]
    assert isinstance(events, list)
    if event.startswith(_WATCHED):
        events.append(event)
    elif (
        event == "open"
        and len(args) >= 2
        and isinstance(args[1], str)
        and any(flag in args[1] for flag in ("w", "a", "x", "+"))
    ):
        events.append(f"open-for-write:{args[0]!s}")


sys.addaudithook(_audit)  # cannot be removed: inert unless a test switches it on


async def test_a_full_graph_run_spawns_nothing_connects_nowhere_writes_no_file() -> None:
    provider = MockModelProvider(dimensions=8)
    provider.register(
        CriticAssessment,
        {"critiques": [{"ref": "c0", "verdict": "accept"}, {"ref": "c1", "verdict": "reject"}]},
    )
    _AUDIT["events"] = []
    _AUDIT["on"] = True
    try:
        result = await run_source(
            FakeBackend.ready([make_plan(0), make_plan(1)]),
            FakeStore(),
            provider,
            make_policy(planner=False, critic=True),
        )
    finally:
        _AUDIT["on"] = False
    assert result.outcome is AgentOutcome.RECOMMEND
    assert _AUDIT["events"] == [], _AUDIT["events"]


def test_the_audit_hook_would_catch_a_subprocess() -> None:
    """The runtime check is not vacuous: a real spawn attempt is recorded."""
    import subprocess

    _AUDIT["events"] = []
    _AUDIT["on"] = True
    try:
        subprocess.run(["/usr/bin/true"], check=False)
    finally:
        _AUDIT["on"] = False
    events = _AUDIT["events"]
    assert isinstance(events, list) and "subprocess.Popen" in events

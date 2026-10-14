"""Static and runtime boundaries of the agent runtime (ADR-0012 sections 6, 7 and 9)."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import socket
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
)
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
def test_no_shell_no_human_actor_no_approval(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in {
                ("os", "system"),
                ("os", "popen"),
                ("os", "exec"),
                ("os", "spawn"),
            }
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

"""MockCMSProvider: the CMS PROTOCOL rules (idempotency, optimistic locking,
dry run, protected pages, audit, rollback). Not HTML mutation -- that is Phase 6."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from ken_contracts.cms import (
    CMSNotFoundError,
    CMSPage,
    CMSProvider,
    CMSUnavailableError,
    CMSValidationError,
    CMSVersionConflictError,
    LinkChange,
    LinkChangeRequest,
    LinkLocator,
    LinkOperation,
)
from ken_core.providers.cms.mock import IssueCode, MockCMSProvider
from ken_core.providers.cms.seed import (
    INDIA_EV_MARKET_PAGE_ID as SOURCE,
)
from ken_core.providers.cms.seed import (
    RESEARCH_METHODOLOGY_PAGE_ID as PROTECTED,
)
from ken_core.providers.cms.seed import (
    SEA_EV_MARKET_PAGE_ID as TARGET,
)
from ken_core.providers.cms.seed import (
    SEED_PAGE_IDS,
    seed_pages,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "cms" / "pages"
TARGET_HREF = (
    "https://example.com/industry-reports/southeast-asia-electric-vehicle-market-outlook-to-2030"
)


# --------------------------------------------------------------------------- #
# Helpers (no conftest: file ownership)
# --------------------------------------------------------------------------- #
@pytest.fixture
def provider() -> MockCMSProvider:
    return MockCMSProvider(seed_pages())


def _change(
    *,
    operation: LinkOperation = LinkOperation.INSERT,
    target: str = TARGET,
    change_id: UUID | None = None,
    previous_href: str | None = None,
    previous_anchor_text: str | None = None,
) -> LinkChange:
    return LinkChange(
        change_id=change_id or uuid4(),
        operation=operation,
        target_page_id=target,
        href=TARGET_HREF,
        anchor_text="Southeast Asian markets",
        locator=LinkLocator(block_selector="p#p-regional"),
        previous_href=previous_href,
        previous_anchor_text=previous_anchor_text,
    )


def _request(
    *,
    source: str = SOURCE,
    expected_version: str = "seed-v1",
    changes: list[LinkChange] | None = None,
    operation_id: UUID | None = None,
    dry_run: bool = False,
    actor: str = "editor@example.com",
) -> LinkChangeRequest:
    return LinkChangeRequest(
        operation_id=operation_id or uuid4(),
        source_page_id=source,
        expected_version=expected_version,
        changes=changes if changes is not None else [_change()],
        dry_run=dry_run,
        actor=actor,
    )


def _codes(issues: Any) -> set[str]:
    return {i.code for i in issues}


async def _snapshot(p: MockCMSProvider) -> dict[str, CMSPage]:
    return {pid: await p.get_page(page_id=pid) for pid in SEED_PAGE_IDS}


# --------------------------------------------------------------------------- #
# Construction, protocol, get_page, health
# --------------------------------------------------------------------------- #
def test_conforms_to_cms_provider_protocol(provider: MockCMSProvider) -> None:
    assert isinstance(provider, CMSProvider)
    assert provider.name == "mock"


def test_duplicate_page_ids_rejected() -> None:
    page = seed_pages()[0]
    with pytest.raises(ValueError, match="Duplicate"):
        MockCMSProvider([page, page])


async def test_get_page_returns_seed_page(provider: MockCMSProvider) -> None:
    page = await provider.get_page(page_id=SOURCE)
    assert page.page_id == SOURCE
    assert page.url.startswith("https://example.com/")


async def test_get_page_unknown_raises_not_found(provider: MockCMSProvider) -> None:
    with pytest.raises(CMSNotFoundError):
        await provider.get_page(page_id="no-such-page")


async def test_get_page_returns_a_copy(provider: MockCMSProvider) -> None:
    page = await provider.get_page(page_id=SOURCE)
    page.body_html = "tampered"
    page.version = "tampered"
    fresh = await provider.get_page(page_id=SOURCE)
    assert fresh.body_html != "tampered"
    assert fresh.version == "seed-v1"


async def test_constructor_copies_input_pages() -> None:
    pages = seed_pages()
    p = MockCMSProvider(pages)
    pages[0].version = "tampered"
    assert (await p.get_page(page_id=pages[0].page_id)).version == "seed-v1"


async def test_from_fixture_dir_loads_every_page() -> None:
    p = MockCMSProvider.from_fixture_dir(FIXTURE_DIR)
    for page_id in SEED_PAGE_IDS:
        assert (await p.get_page(page_id=page_id)).page_id == page_id
    assert (await p.get_page(page_id=PROTECTED)).protected is True


async def test_fixture_pages_mirror_seed_pages() -> None:
    """Drift guard: the JSON fixtures and the Python seed describe the same pages."""
    p = MockCMSProvider.from_fixture_dir(FIXTURE_DIR)
    for seed in seed_pages():
        assert await p.get_page(page_id=seed.page_id) == seed


def test_from_fixture_dir_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        MockCMSProvider.from_fixture_dir(tmp_path / "absent")


def test_from_fixture_dir_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No"):
        MockCMSProvider.from_fixture_dir(tmp_path)


async def test_health_reports_reachable_mock(provider: MockCMSProvider) -> None:
    health = await provider.health()
    assert health.provider == "mock"
    assert health.reachable is True
    assert health.error is None


async def test_aclose_is_idempotent_and_blocks_further_use(provider: MockCMSProvider) -> None:
    await provider.aclose()
    await provider.aclose()
    health = await provider.health()  # must not raise
    assert health.reachable is False
    with pytest.raises(CMSUnavailableError):
        await provider.get_page(page_id=SOURCE)
    with pytest.raises(CMSUnavailableError):
        await provider.apply_link_changes(request=_request())


# --------------------------------------------------------------------------- #
# validate_link_changes
# --------------------------------------------------------------------------- #
async def test_validate_accepts_valid_request(provider: MockCMSProvider) -> None:
    change = _change()
    result = await provider.validate_link_changes(request=_request(changes=[change]))
    assert result.valid is True
    assert result.issues == []
    assert result.normalized_changes == [change]


async def test_validate_never_mutates(provider: MockCMSProvider) -> None:
    before = await _snapshot(provider)
    requests = [
        _request(),
        _request(expected_version="stale"),
        _request(source=PROTECTED),
        _request(changes=[_change(target=SOURCE)]),
    ]
    for request in requests:
        await provider.validate_link_changes(request=request)
    assert await _snapshot(provider) == before
    assert provider.audit_log == ()
    # Validation did not consume the operation_id either.
    result = await provider.apply_link_changes(request=requests[0])
    assert result.status == "applied"
    assert result.idempotent_replay is False


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"source": "no-such-page"}, IssueCode.UNKNOWN_SOURCE_PAGE),
        ({"expected_version": "some-other-token"}, IssueCode.STALE_PAGE_VERSION),
        ({"source": PROTECTED}, IssueCode.PROTECTED_PAGE),
        ({"actor": "   "}, IssueCode.MISSING_ACTOR),
    ],
    ids=["unknown-source", "stale-version", "protected", "blank-actor"],
)
async def test_validate_request_level_issues(
    provider: MockCMSProvider, kwargs: dict[str, Any], code: IssueCode
) -> None:
    result = await provider.validate_link_changes(request=_request(**kwargs))
    assert result.valid is False
    assert code in _codes(result.issues)
    assert all(i.severity == "error" for i in result.issues)
    assert result.normalized_changes == []


@pytest.mark.parametrize(
    ("change_kwargs", "code"),
    [
        ({"target": "no-such-page"}, IssueCode.UNKNOWN_TARGET_PAGE),
        ({"target": SOURCE}, IssueCode.SELF_LINK),
        ({"previous_href": "https://example.com/old"}, IssueCode.INSERT_HAS_PREVIOUS_FIELDS),
        ({"previous_anchor_text": "old"}, IssueCode.INSERT_HAS_PREVIOUS_FIELDS),
        (
            {"operation": LinkOperation.REMOVE, "previous_anchor_text": "old"},
            IssueCode.MISSING_PREVIOUS_HREF,
        ),
        (
            {"operation": LinkOperation.REPLACE, "previous_anchor_text": "old"},
            IssueCode.MISSING_PREVIOUS_HREF,
        ),
        (
            {"operation": LinkOperation.REMOVE, "previous_href": "https://example.com/old"},
            IssueCode.MISSING_PREVIOUS_ANCHOR_TEXT,
        ),
    ],
    ids=[
        "unknown-target",
        "self-link",
        "insert-prev-href",
        "insert-prev-anchor",
        "remove-no-prev-href",
        "replace-no-prev-href",
        "remove-no-prev-anchor",
    ],
)
async def test_validate_change_level_issues(
    provider: MockCMSProvider, change_kwargs: dict[str, Any], code: IssueCode
) -> None:
    bad = _change(**change_kwargs)
    good = _change()
    result = await provider.validate_link_changes(request=_request(changes=[good, bad]))
    assert result.valid is False
    issue = next(i for i in result.issues if i.code == code)
    assert issue.change_id == bad.change_id
    # normalized_changes echoes only the valid change.
    assert result.normalized_changes == [good]


@pytest.mark.parametrize("operation", [LinkOperation.REMOVE, LinkOperation.REPLACE])
async def test_validate_accepts_well_formed_remove_and_replace(
    provider: MockCMSProvider, operation: LinkOperation
) -> None:
    change = _change(
        operation=operation,
        previous_href="https://example.com/old",
        previous_anchor_text="old anchor",
    )
    result = await provider.validate_link_changes(request=_request(changes=[change]))
    assert result.valid is True, result.issues


async def test_validate_duplicate_change_id(provider: MockCMSProvider) -> None:
    cid = uuid4()
    first, second = _change(change_id=cid), _change(change_id=cid)
    result = await provider.validate_link_changes(request=_request(changes=[first, second]))
    assert result.valid is False
    dupes = [i for i in result.issues if i.code == IssueCode.DUPLICATE_CHANGE_ID]
    assert len(dupes) == 1
    assert dupes[0].change_id == cid
    assert result.normalized_changes == [first]


# --------------------------------------------------------------------------- #
# apply_link_changes
# --------------------------------------------------------------------------- #
async def test_apply_success(provider: MockCMSProvider) -> None:
    before = await provider.get_page(page_id=SOURCE)
    changes = [_change(), _change()]
    request = _request(changes=changes)
    result = await provider.apply_link_changes(request=request)

    after = await provider.get_page(page_id=SOURCE)
    assert result.status == "applied"
    assert result.idempotent_replay is False
    assert result.operation_id == request.operation_id
    assert result.source_page_id == SOURCE
    assert result.applied_change_ids == [c.change_id for c in changes]
    assert result.previous_version == before.version
    assert result.new_version == after.version
    assert after.version != before.version
    assert result.deployment_id


async def test_apply_never_modifies_body_html(provider: MockCMSProvider) -> None:
    """The mock validates the protocol; anchor insertion is a Phase 6 service."""
    before = await _snapshot(provider)
    await provider.apply_link_changes(request=_request())
    after = await _snapshot(provider)
    for page_id in SEED_PAGE_IDS:
        assert after[page_id].body_html == before[page_id].body_html
    # Only the source page moved; the target page is untouched.
    assert after[TARGET] == before[TARGET]


async def test_apply_uses_injected_clock() -> None:
    fixed = datetime(2026, 5, 1, tzinfo=UTC)
    p = MockCMSProvider(seed_pages(), clock=lambda: fixed)
    result = await p.apply_link_changes(request=_request())
    assert result.applied_at == fixed
    assert (await p.get_page(page_id=SOURCE)).updated_at == fixed


async def test_idempotent_replay_returns_identical_result(provider: MockCMSProvider) -> None:
    request = _request()
    first = await provider.apply_link_changes(request=request)
    version_after_first = (await provider.get_page(page_id=SOURCE)).version

    replay = await provider.apply_link_changes(request=request.model_copy(deep=True))

    assert replay.idempotent_replay is True
    assert replay.model_dump(exclude={"idempotent_replay"}) == first.model_dump(
        exclude={"idempotent_replay"}
    )
    assert (await provider.get_page(page_id=SOURCE)).version == version_after_first
    assert [e.outcome for e in provider.audit_log] == ["applied", "idempotent_replay"]


async def test_idempotent_replay_succeeds_even_after_page_moved(
    provider: MockCMSProvider,
) -> None:
    request = _request()
    first = await provider.apply_link_changes(request=request)
    # Another operation moves the page on.
    second = await provider.apply_link_changes(request=_request(expected_version=first.new_version))
    replay = await provider.apply_link_changes(request=request)
    assert replay.idempotent_replay is True
    assert replay.deployment_id == first.deployment_id
    assert (await provider.get_page(page_id=SOURCE)).version == second.new_version


async def test_operation_id_reuse_with_different_request_rejected(
    provider: MockCMSProvider,
) -> None:
    request = _request()
    first = await provider.apply_link_changes(request=request)
    different = _request(operation_id=request.operation_id, expected_version=first.new_version)
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=different)
    assert _codes(exc.value.issues) == {IssueCode.OPERATION_ID_REUSED}
    assert (await provider.get_page(page_id=SOURCE)).version == first.new_version


async def test_version_conflict_carries_expected_and_current(
    provider: MockCMSProvider,
) -> None:
    with pytest.raises(CMSVersionConflictError) as exc:
        await provider.apply_link_changes(request=_request(expected_version="stale-token"))
    assert exc.value.expected == "stale-token"
    assert exc.value.current == "seed-v1"
    assert (await provider.get_page(page_id=SOURCE)).version == "seed-v1"
    assert provider.audit_log[-1].outcome == "rejected_version_conflict"


async def test_version_conflict_takes_precedence_over_change_errors(
    provider: MockCMSProvider,
) -> None:
    request = _request(expected_version="stale-token", changes=[_change(target="no-such-page")])
    with pytest.raises(CMSVersionConflictError):
        await provider.apply_link_changes(request=request)


async def test_rejected_operation_id_is_not_consumed(provider: MockCMSProvider) -> None:
    op = uuid4()
    with pytest.raises(CMSVersionConflictError):
        await provider.apply_link_changes(request=_request(operation_id=op, expected_version="x"))
    result = await provider.apply_link_changes(request=_request(operation_id=op))
    assert result.status == "applied"


async def test_apply_validation_error_raises_with_issues(provider: MockCMSProvider) -> None:
    before = await _snapshot(provider)
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=_request(changes=[_change(target=SOURCE)]))
    assert IssueCode.SELF_LINK in _codes(exc.value.issues)
    assert await _snapshot(provider) == before
    assert provider.audit_log[-1].outcome == "rejected_validation"


async def test_apply_unknown_source_page_rejected(provider: MockCMSProvider) -> None:
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=_request(source="no-such-page"))
    assert IssueCode.UNKNOWN_SOURCE_PAGE in _codes(exc.value.issues)


@pytest.mark.parametrize("dry_run", [False, True])
async def test_protected_page_cannot_be_mutated_even_when_otherwise_valid(
    provider: MockCMSProvider, dry_run: bool
) -> None:
    before = await provider.get_page(page_id=PROTECTED)
    # Correct version, valid target, valid fields: protection alone must block it.
    request = _request(source=PROTECTED, expected_version=before.version, dry_run=dry_run)
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=request)
    assert _codes(exc.value.issues) == {IssueCode.PROTECTED_PAGE}
    assert await provider.get_page(page_id=PROTECTED) == before


async def test_protected_page_rejected_before_version_conflict(
    provider: MockCMSProvider,
) -> None:
    """Regenerating cannot make a protected page writable, so don't send a 409."""
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=_request(source=PROTECTED, expected_version="x"))
    assert IssueCode.PROTECTED_PAGE in _codes(exc.value.issues)


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #
async def test_dry_run_persists_nothing_and_does_not_consume_operation_id(
    provider: MockCMSProvider,
) -> None:
    before = await _snapshot(provider)
    request = _request(dry_run=True)

    result = await provider.apply_link_changes(request=request)

    assert result.status == "no_op"
    assert result.new_version == result.previous_version == "seed-v1"
    assert result.applied_change_ids == []
    assert await _snapshot(provider) == before
    assert provider.audit_log == ()
    # A dry-run deployment is not a rollback handle.
    with pytest.raises(CMSNotFoundError):
        await provider.rollback(
            deployment_id=result.deployment_id,
            operation_id=request.operation_id,
            actor="editor@example.com",
        )
    # The same operation_id can now be applied for real.
    real = await provider.apply_link_changes(request=request.model_copy(update={"dry_run": False}))
    assert real.status == "applied"
    assert real.idempotent_replay is False


async def test_dry_run_validates_fully(provider: MockCMSProvider) -> None:
    with pytest.raises(CMSVersionConflictError):
        await provider.apply_link_changes(request=_request(dry_run=True, expected_version="x"))
    with pytest.raises(CMSValidationError):
        await provider.apply_link_changes(
            request=_request(dry_run=True, changes=[_change(target="no-such-page")])
        )
    assert provider.audit_log == ()


async def test_dry_run_reports_operation_id_reuse(provider: MockCMSProvider) -> None:
    request = _request()
    first = await provider.apply_link_changes(request=request)

    same = await provider.apply_link_changes(request=request.model_copy(update={"dry_run": True}))
    assert same.status == "no_op"
    assert same.idempotent_replay is True
    assert same.deployment_id == first.deployment_id
    assert same.new_version == same.previous_version == first.new_version

    different = _request(
        operation_id=request.operation_id, expected_version=first.new_version, dry_run=True
    )
    with pytest.raises(CMSValidationError) as exc:
        await provider.apply_link_changes(request=different)
    assert _codes(exc.value.issues) == {IssueCode.OPERATION_ID_REUSED}


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
async def test_concurrent_applies_with_same_expected_version_exactly_one_wins(
    provider: MockCMSProvider,
) -> None:
    requests = [_request() for _ in range(2)]
    outcomes = await asyncio.gather(
        *(provider.apply_link_changes(request=r) for r in requests), return_exceptions=True
    )
    wins = [o for o in outcomes if not isinstance(o, BaseException)]
    conflicts = [o for o in outcomes if isinstance(o, CMSVersionConflictError)]
    assert len(wins) == 1
    assert len(conflicts) == 1
    assert conflicts[0].expected == "seed-v1"
    assert conflicts[0].current == wins[0].new_version
    assert (await provider.get_page(page_id=SOURCE)).version == wins[0].new_version


async def test_concurrent_identical_replays_apply_once(provider: MockCMSProvider) -> None:
    request = _request()
    results = await asyncio.gather(
        *(provider.apply_link_changes(request=request) for _ in range(5))
    )
    assert {r.deployment_id for r in results} == {results[0].deployment_id}
    assert sum(not r.idempotent_replay for r in results) == 1
    assert [e.outcome for e in provider.audit_log].count("applied") == 1


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
async def test_rollback_restores_page_with_new_version(provider: MockCMSProvider) -> None:
    before = await provider.get_page(page_id=SOURCE)
    request = _request()
    deployment = await provider.apply_link_changes(request=request)

    result = await provider.rollback(
        deployment_id=deployment.deployment_id,
        operation_id=request.operation_id,
        actor="lead@example.com",
    )

    after = await provider.get_page(page_id=SOURCE)
    assert result.status == "rolled_back"
    assert result.actor == "lead@example.com"
    assert result.deployment_id == deployment.deployment_id
    # Restored content, but a NEW version token: a rollback is a new state.
    assert after.version == result.restored_version
    assert result.restored_version not in {before.version, deployment.new_version}
    assert after.model_dump(exclude={"version", "updated_at"}) == before.model_dump(
        exclude={"version", "updated_at"}
    )
    # A plan built against the pre-deployment token must not pass the lock.
    with pytest.raises(CMSVersionConflictError):
        await provider.apply_link_changes(request=_request(expected_version=before.version))


async def test_second_rollback_is_no_op(provider: MockCMSProvider) -> None:
    request = _request()
    deployment = await provider.apply_link_changes(request=request)
    first = await provider.rollback(
        deployment_id=deployment.deployment_id,
        operation_id=request.operation_id,
        actor="lead@example.com",
    )
    version_after_first = (await provider.get_page(page_id=SOURCE)).version

    second = await provider.rollback(
        deployment_id=deployment.deployment_id,
        operation_id=request.operation_id,
        actor="other@example.com",
    )

    assert second.status == "no_op"
    assert second.restored_version == first.restored_version
    assert second.rolled_back_at == first.rolled_back_at
    assert second.actor == first.actor  # reports the original authoriser
    assert (await provider.get_page(page_id=SOURCE)).version == version_after_first
    rollback_entries = [e for e in provider.audit_log if e.action == "rollback"]
    assert [(e.outcome, e.actor) for e in rollback_entries] == [
        ("rolled_back", "lead@example.com"),
        ("rollback_no_op", "other@example.com"),
    ]


async def test_rollback_unknown_deployment_raises_not_found(
    provider: MockCMSProvider,
) -> None:
    with pytest.raises(CMSNotFoundError):
        await provider.rollback(
            deployment_id="dep-unknown", operation_id=uuid4(), actor="lead@example.com"
        )
    assert provider.audit_log[-1].outcome == "rejected_not_found"


async def test_rollback_with_wrong_operation_id_rejected(provider: MockCMSProvider) -> None:
    deployment = await provider.apply_link_changes(request=_request())
    version = (await provider.get_page(page_id=SOURCE)).version
    with pytest.raises(CMSValidationError) as exc:
        await provider.rollback(
            deployment_id=deployment.deployment_id,
            operation_id=uuid4(),
            actor="lead@example.com",
        )
    assert _codes(exc.value.issues) == {IssueCode.DEPLOYMENT_OPERATION_MISMATCH}
    assert (await provider.get_page(page_id=SOURCE)).version == version


async def test_rollback_requires_actor(provider: MockCMSProvider) -> None:
    request = _request()
    deployment = await provider.apply_link_changes(request=request)
    with pytest.raises(CMSValidationError) as exc:
        await provider.rollback(
            deployment_id=deployment.deployment_id,
            operation_id=request.operation_id,
            actor=" ",
        )
    assert _codes(exc.value.issues) == {IssueCode.MISSING_ACTOR}


async def test_rollback_without_an_engine_refuses_after_a_later_deployment(
    provider: MockCMSProvider,
) -> None:
    """ADR-0014 s8 supersedes Phase 0's "page moved -> CMSVersionConflictError": without an
    HTML engine nothing can prove the later change unrelated, so the refusal is the typed
    ``rollback_conflict`` and the page is untouched. (With an engine the inverse splice runs:
    tests/unit/deployment/test_cms_providers_contract.py.)"""
    first_request = _request()
    first = await provider.apply_link_changes(request=first_request)
    second = await provider.apply_link_changes(request=_request(expected_version=first.new_version))
    with pytest.raises(CMSValidationError) as exc:
        await provider.rollback(
            deployment_id=first.deployment_id,
            operation_id=first_request.operation_id,
            actor="lead@example.com",
        )
    assert _codes(exc.value.issues) == {IssueCode.ROLLBACK_CONFLICT}
    assert (await provider.get_page(page_id=SOURCE)).version == second.new_version


async def test_replay_after_rollback_does_not_reapply(provider: MockCMSProvider) -> None:
    request = _request()
    deployment = await provider.apply_link_changes(request=request)
    rollback = await provider.rollback(
        deployment_id=deployment.deployment_id,
        operation_id=request.operation_id,
        actor="lead@example.com",
    )
    replay = await provider.apply_link_changes(request=request)
    assert replay.idempotent_replay is True
    assert (await provider.get_page(page_id=SOURCE)).version == rollback.restored_version


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
async def test_audit_log_records_apply_and_rollback(provider: MockCMSProvider) -> None:
    request = _request(actor="editor@example.com")
    deployment = await provider.apply_link_changes(request=request)
    await provider.rollback(
        deployment_id=deployment.deployment_id,
        operation_id=request.operation_id,
        actor="lead@example.com",
    )
    apply_entry, rollback_entry = provider.audit_log
    assert (apply_entry.action, apply_entry.outcome) == ("apply", "applied")
    assert apply_entry.operation_id == request.operation_id
    assert apply_entry.actor == "editor@example.com"
    assert apply_entry.deployment_id == deployment.deployment_id
    assert apply_entry.page_id == SOURCE
    assert (rollback_entry.action, rollback_entry.outcome) == ("rollback", "rolled_back")
    assert rollback_entry.actor == "lead@example.com"
    assert rollback_entry.at >= apply_entry.at


async def test_audit_log_is_read_only(provider: MockCMSProvider) -> None:
    await provider.apply_link_changes(request=_request())
    log = provider.audit_log
    assert isinstance(log, tuple)
    with pytest.raises(AttributeError):
        log[0].actor = "forged"  # type: ignore[misc]
    assert provider.audit_log == log

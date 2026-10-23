"""Governance hardening: a skill is promoted only by the promotion service, on a passing run.

Revision ID: 0011_governance_hardening
Revises: 0010_qdrant_and_autonomy
Create Date: 2026-10-18

QA finding: ``ken_skill_guard()`` (0009) accepted ``offline_test -> promoted`` with a
``user:`` promoter and ANY non-null ``evaluation_run_id``. Raw SQL could therefore promote a
skill on a FAILING evaluation (or one belonging to another skill), with no promoter setting.
The proposals path already needs the transaction-local ``ken.learning_promoter`` setting; the
skills path did not.

This migration replaces the function (``CREATE OR REPLACE``; the triggers stay as 0009 made
them, so no table, column or trigger changes). A transition to ``promoted`` now ALSO requires:

* the transaction-local setting ``ken.learning_promoter`` to be ``on`` -- set only by
  ``PromotionService`` in the same transaction as the transition;
* the referenced ``evaluation_run_id`` to exist in ``policy_evaluations`` with
  ``gate_result = 'pass'`` AND ``skill_id`` equal to THIS skill;
* and, unchanged, a human ``user:`` promoter with ``promoted_at`` set.

Downgrade restores the 0009 function body exactly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0011_governance_hardening"
down_revision: str | Sequence[str] | None = "0010_qdrant_and_autonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROMOTER_SETTING: Final = "ken.learning_promoter"
_PROMOTER_RE: Final = "'^user:[a-z0-9][a-z0-9._-]{2,63}$'"
_MUTABLE: Final = (
    "ARRAY['status', 'promoted_by', 'promoted_at', 'evaluation_run_id', "
    "'evaluation_score']::text[]"
)
_TRANSITIONS: Final = (
    "ARRAY['draft>offline_test', 'draft>rejected', 'offline_test>promoted', "
    "'offline_test>rejected', 'promoted>retired']::text[]"
)

_HEAD: Final = f"""
        CREATE OR REPLACE FUNCTION ken_skill_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'table % rows are never deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - {_MUTABLE})
                IS DISTINCT FROM (to_jsonb(OLD) - {_MUTABLE}) THEN
                RAISE EXCEPTION 'skills content is immutable: a revision is a new version row'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
                AND NOT ((OLD.status || '>' || NEW.status) = ANY ({_TRANSITIONS}))
            THEN
                RAISE EXCEPTION 'skills status % -> % is not allowed', OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status = 'promoted' AND (
                NEW.promoted_by IS NULL OR NEW.promoted_at IS NULL
                OR NEW.evaluation_run_id IS NULL
                OR NEW.promoted_by !~ {_PROMOTER_RE}
            ) THEN
                RAISE EXCEPTION
                    'a promoted skill needs a human promoter and a passing evaluation'
                    USING ERRCODE = '55000';
            END IF;
"""

_TAIL: Final = """            RETURN NEW;
        END;
        $$
"""

_NEW_CHECKS: Final = f"""
            IF NEW.status = 'promoted' AND NEW.status IS DISTINCT FROM OLD.status THEN
                IF coalesce(current_setting('{PROMOTER_SETTING}', true), '') <> 'on' THEN
                    RAISE EXCEPTION
                        'promoting a skill requires the promotion service'
                        USING ERRCODE = '55000';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM policy_evaluations e
                    WHERE e.id = NEW.evaluation_run_id
                      AND e.gate_result = 'pass'
                      AND e.skill_id = NEW.id
                ) THEN
                    RAISE EXCEPTION
                        'a promoted skill needs a passing evaluation of this skill'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
"""


def upgrade() -> None:
    op.execute(_HEAD + _NEW_CHECKS + _TAIL)


def downgrade() -> None:
    op.execute(_HEAD + _TAIL)

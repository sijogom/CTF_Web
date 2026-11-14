"""Fail-closed schema contracts for the Auth migration bootstrap path.

Auth existed before its Alembic history.  The first revision therefore marks a
*verified* legacy core rather than recreating it.  Keep the baseline and the
post-head contract separate: a legacy database must not be compared with the
current model schema before forward migrations have had a chance to run.

The functions in this module are deliberately pure.  The migration wrapper
collects SQLAlchemy inspection data and passes it here, which keeps the
classification and contract rules testable without settings, a database, or
environment files.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum


ALEMBIC_VERSION_TABLE = "alembic_version"
ADMIN_SETTINGS_TABLE = "admin_settings"

# These tables existed before the Auth Alembic baseline.  A database that has
# only part of this set is ambiguous and must be restored or repaired under an
# explicit operator procedure rather than adopted automatically at deployment.
LEGACY_CORE_TABLES = frozenset(
    {
        "users",
        "password_resets",
        "email_logs",
        "teams",
        "team_members",
        "notifications",
        "audit_logs",
        "competitions",
        "competition_challenges",
        "competition_assignments",
        "competition_solves",
        "competition_participants",
    }
)

# Baseline validation intentionally excludes columns introduced by later
# forward migrations (for example Discord identity, email-code verification,
# scoring, and soft deletion).  This lets a complete historical core enter at
# revision 000 and lets revisions 001--003 own their additions.
LEGACY_CORE_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset(
        {
            "id",
            "ctfd_user_id",
            "email",
            "username",
            "password_hash",
            "oauth_provider",
            "oauth_id",
            "role",
            "level",
            "xp",
            "points",
            "streak_days",
            "avatar_url",
            "bio",
            "totp_secret",
            "totp_enabled",
            "backup_codes",
            "is_active",
            "email_verified",
            "email_verify_token",
            "email_verify_expires",
            "last_active_at",
            "created_at",
            "updated_at",
        }
    ),
    "password_resets": frozenset(
        {"id", "user_id", "token", "used", "expires_at", "created_at"}
    ),
    "email_logs": frozenset(
        {
            "id",
            "sender_id",
            "recipients",
            "subject",
            "target_type",
            "total_count",
            "success_count",
            "fail_count",
            "error_details",
            "created_at",
        }
    ),
    "teams": frozenset(
        {
            "id",
            "name",
            "description",
            "avatar_url",
            "invite_code",
            "captain_id",
            "total_score",
            "created_at",
        }
    ),
    "team_members": frozenset({"team_id", "user_id", "joined_at"}),
    "notifications": frozenset(
        {"id", "title", "content", "type", "created_by", "created_at"}
    ),
    "audit_logs": frozenset(
        {
            "id",
            "actor_id",
            "action",
            "target_type",
            "target_id",
            "details",
            "ip_address",
            "created_at",
        }
    ),
    "competitions": frozenset(
        {
            "id",
            "title",
            "description",
            "mode",
            "start_at",
            "end_at",
            "freeze_minutes",
            "banner_url",
            "difficulty_distribution",
            "is_published",
            "created_by",
            "ctfd_event_id",
            "created_at",
        }
    ),
    "competition_challenges": frozenset(
        {"competition_id", "challenge_id", "category", "difficulty"}
    ),
    "competition_assignments": frozenset(
        {"competition_id", "user_id", "category", "challenge_ids", "assigned_at"}
    ),
    "competition_solves": frozenset(
        {"competition_id", "user_id", "challenge_id", "solved_at"}
    ),
    "competition_participants": frozenset(
        {"competition_id", "user_id", "joined_at", "score"}
    ),
}

# Revision 001 creates these atomically.  A lone table is evidence of an
# interrupted/hand-edited schema, never a legacy baseline to adopt.
BAN_LEDGER_TABLES = frozenset({"account_bans", "ban_appeals"})

_BAN_LEDGER_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "account_bans": frozenset(
        {
            "id",
            "user_id",
            "public_reason",
            "evidence_summary",
            "policy_version",
            "source",
            "detection_rule_version",
            "started_at",
            "expires_at",
            "created_by",
            "created_at",
            "lifted_at",
            "lifted_by",
            "lift_reason",
            "appeal_allowed",
        }
    ),
    "ban_appeals": frozenset(
        {
            "id",
            "ban_id",
            "user_id",
            "message",
            "status",
            "reviewer_id",
            "reviewer_note",
            "reviewed_at",
            "created_at",
        }
    ),
}

_RUNTIME_MIGRATION_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset(
        {
            "discord_id",
            "discord_username",
            "baekjoon_handle",
            "baekjoon_tier",
            "tier_override",
            "email_notifications",
            "email_verify_code_required",
            "email_verify_code_hash",
            "email_verify_code_expires",
            "email_verify_code_sent_at",
            "email_verify_code_attempts",
        }
    ),
    "competition_challenges": frozenset(
        {"scoring_type", "initial_value", "decay", "minimum_value"}
    ),
    "competitions": frozenset(
        {
            "is_deleted",
            "deleted_at",
            "deleted_by",
            "deletion_reason",
            "published_before_deletion",
            "team_contract",
        }
    ),
    ADMIN_SETTINGS_TABLE: frozenset({"key", "value", "updated_at"}),
    "competition_deletion_confirmations": frozenset(
        {
            "id",
            "competition_id",
            "actor_id",
            "token_hash",
            "reason",
            "expires_at",
            "used_at",
            "created_at",
        }
    ),
    "inquiry_notification_receipts": frozenset(
        {
            "id",
            "event_id",
            "status",
            "attempt_count",
            "lease_expires_at",
            "terminal_at",
            "last_error",
            "created_at",
            "updated_at",
        }
    ),
    "competition_team_registrations": frozenset(
        {"competition_id", "team_id", "team_name_snapshot", "registered_by", "registered_at"}
    ),
    "competition_team_members": frozenset(
        {"competition_id", "team_id", "user_id", "username_snapshot"}
    ),
    "competition_team_assignments": frozenset(
        {"competition_id", "team_id", "category", "challenge_ids", "assigned_at"}
    ),
    "competition_team_solves": frozenset(
        {"competition_id", "team_id", "challenge_id", "solved_at", "first_solver_id"}
    ),
}


def _merge_schema_contracts(
    *contracts: Mapping[str, Collection[str]],
) -> dict[str, frozenset[str]]:
    merged: dict[str, frozenset[str]] = {}
    for contract in contracts:
        for table_name, column_names in contract.items():
            merged[table_name] = merged.get(table_name, frozenset()) | frozenset(
                column_names
            )
    return merged


# This is deliberately a stable, explicit post-head compatibility contract,
# rather than every currently registered SQLAlchemy column.  New additive
# migrations remain possible; a new runtime-required table/column must add its
# own migration and extend this contract in the same change.
CURRENT_AUTH_REQUIRED_COLUMNS = _merge_schema_contracts(
    LEGACY_CORE_REQUIRED_COLUMNS,
    _BAN_LEDGER_REQUIRED_COLUMNS,
    _RUNTIME_MIGRATION_REQUIRED_COLUMNS,
)
CURRENT_AUTH_REQUIRED_TABLES = frozenset(CURRENT_AUTH_REQUIRED_COLUMNS)


class AuthSchemaPreparation(StrEnum):
    BOOTSTRAP_EMPTY = "bootstrap_empty"
    ADOPT_COMPLETE_LEGACY = "adopt_complete_legacy"
    UPGRADE_EXISTING = "upgrade_existing"


def _validate_schema_contract(
    *,
    table_columns: Mapping[str, Collection[str]],
    required_columns: Mapping[str, Collection[str]],
    contract_name: str,
) -> None:
    actual_tables = set(table_columns)
    missing_tables = sorted(set(required_columns) - actual_tables)
    if missing_tables:
        raise RuntimeError(
            f"Auth {contract_name}에 필요한 테이블이 없습니다: "
            f"{', '.join(missing_tables)}."
        )

    missing_columns: list[str] = []
    for table_name, expected_columns in required_columns.items():
        actual_columns = set(table_columns[table_name])
        for column_name in sorted(set(expected_columns) - actual_columns):
            missing_columns.append(f"{table_name}.{column_name}")
    if missing_columns:
        raise RuntimeError(
            f"Auth {contract_name}에 필요한 컬럼이 없습니다: "
            f"{', '.join(missing_columns[:16])}."
        )


def validate_legacy_core_schema(
    table_columns: Mapping[str, Collection[str]],
) -> None:
    """Verify only revision-000 legacy-core requirements before stamping."""

    _validate_schema_contract(
        table_columns=table_columns,
        required_columns=LEGACY_CORE_REQUIRED_COLUMNS,
        contract_name="legacy baseline(20260813_000)",
    )


def validate_current_auth_schema(
    table_columns: Mapping[str, Collection[str]],
) -> None:
    """Verify the stable runtime-required schema after ``upgrade head``."""

    _validate_schema_contract(
        table_columns=table_columns,
        required_columns=CURRENT_AUTH_REQUIRED_COLUMNS,
        contract_name="post-head schema",
    )


def classify_auth_schema_tables(
    *,
    table_names: Collection[str],
    application_table_names: Collection[str],
) -> AuthSchemaPreparation:
    """Classify Auth schema state before any DDL is attempted.

    Only an entirely table-free database may use the pristine bootstrap path.
    A complete historical core without an Alembic marker may take the explicit
    legacy-adoption path.  Versioned schemas keep their normal Alembic upgrade
    path.  Unknown tables, incomplete core tables, a marker without the core,
    and a partial ban ledger are all rejected before mutation.
    """

    actual = frozenset(table_names)
    expected = frozenset(application_table_names)
    allowed = expected | {ALEMBIC_VERSION_TABLE, ADMIN_SETTINGS_TABLE}

    if not actual:
        return AuthSchemaPreparation.BOOTSTRAP_EMPTY

    unexpected = actual - allowed
    if unexpected:
        raise RuntimeError(
            "Auth schema bootstrap이 알 수 없는 테이블이 있는 DB를 자동 채택하지 "
            f"않습니다: {', '.join(sorted(unexpected))}."
        )

    present_core = actual.intersection(LEGACY_CORE_TABLES)
    if present_core != LEGACY_CORE_TABLES:
        raise RuntimeError(
            "Auth core schema가 부분 생성 상태입니다. "
            f"현재 {', '.join(sorted(present_core)) or '없음'}, "
            f"필수 {', '.join(sorted(LEGACY_CORE_TABLES))}."
        )

    present_ban_ledger = actual.intersection(BAN_LEDGER_TABLES)
    if present_ban_ledger and present_ban_ledger != BAN_LEDGER_TABLES:
        raise RuntimeError(
            "Auth ban ledger가 부분 생성 상태입니다: "
            f"{', '.join(sorted(present_ban_ledger))}."
        )

    if ALEMBIC_VERSION_TABLE in actual:
        return AuthSchemaPreparation.UPGRADE_EXISTING
    return AuthSchemaPreparation.ADOPT_COMPLETE_LEGACY

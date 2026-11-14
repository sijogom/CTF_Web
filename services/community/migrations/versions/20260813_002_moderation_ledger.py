"""Create or strictly validate the Community moderation ledger."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_002"
down_revision: Union[str, None] = "20260813_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = frozenset({"moderation_confirmations", "moderation_events"})


def _table_names(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _create_confirmation_table() -> None:
    op.create_table(
        "moderation_confirmations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_name", sa.String(length=300), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('post', 'comment', 'marketplace_item', 'marketplace_comment')",
            name="ck_moderation_confirmation_target_type",
        ),
        sa.CheckConstraint(
            "action IN ('hide', 'restore')",
            name="ck_moderation_confirmation_action",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND consumed_by IS NULL) OR (consumed_at IS NOT NULL AND consumed_by IS NOT NULL)",
            name="ck_moderation_confirmation_consumed_pair",
        ),
    )
    op.create_index(
        "ix_moderation_confirmations_token_digest",
        "moderation_confirmations",
        ["token_digest"],
        unique=True,
    )
    for name, columns in {
        "ix_moderation_confirmations_target_type": ["target_type"],
        "ix_moderation_confirmations_target_id": ["target_id"],
        "ix_moderation_confirmations_requested_by": ["requested_by"],
        "ix_moderation_confirmations_expires_at": ["expires_at"],
        "ix_moderation_confirmation_requester_expiry": ["requested_by", "expires_at"],
    }.items():
        op.create_index(name, "moderation_confirmations", columns)


def _create_event_table() -> None:
    op.create_table(
        "moderation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "confirmation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("moderation_confirmations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_name", sa.String(length=300), nullable=False),
        sa.Column("impact_scope", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("prior_hidden", sa.Boolean(), nullable=False),
        sa.Column("resulting_hidden", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_type IN ('post', 'comment', 'marketplace_item', 'marketplace_comment')",
            name="ck_moderation_event_target_type",
        ),
        sa.CheckConstraint("action IN ('hide', 'restore')", name="ck_moderation_event_action"),
        sa.CheckConstraint(
            "prior_hidden IS DISTINCT FROM resulting_hidden",
            name="ck_moderation_event_state_change",
        ),
    )
    op.create_index(
        "ix_moderation_events_confirmation_id",
        "moderation_events",
        ["confirmation_id"],
        unique=True,
    )
    for name, columns in {
        "ix_moderation_events_target_type": ["target_type"],
        "ix_moderation_events_target_id": ["target_id"],
        "ix_moderation_events_actor_id": ["actor_id"],
        "ix_moderation_events_created_at": ["created_at"],
        "ix_moderation_event_target_created": ["target_type", "target_id", "created_at"],
    }.items():
        op.create_index(name, "moderation_events", columns)


def _assert_existing_contract(bind) -> None:
    inspector = sa.inspect(bind)
    expected_columns = {
        "moderation_confirmations": {
            "id", "token_digest", "target_type", "target_id", "target_name", "target_fingerprint",
            "action", "reason", "requested_by", "issued_at", "expires_at", "consumed_at", "consumed_by",
        },
        "moderation_events": {
            "id", "confirmation_id", "target_type", "target_id", "target_name", "impact_scope", "action",
            "reason", "actor_id", "actor_role", "prior_hidden", "resulting_hidden", "created_at",
        },
    }
    for table_name, expected in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        missing = expected - actual
        if missing:
            raise RuntimeError(
                f"Community moderation ledger table {table_name} lacks columns: {', '.join(sorted(missing))}"
            )

    event_foreign_keys = inspector.get_foreign_keys("moderation_events")
    if not any(
        fk.get("referred_table") == "moderation_confirmations" and fk.get("constrained_columns") == ["confirmation_id"]
        for fk in event_foreign_keys
    ):
        raise RuntimeError("moderation_events.confirmation_id foreign key is missing")

    def unique_column_sets(table_name: str) -> set[tuple[str, ...]]:
        constraints = {
            tuple(sorted(item["column_names"] or ()))
            for item in inspector.get_unique_constraints(table_name)
        }
        indexes = {
            tuple(sorted(item["column_names"] or ()))
            for item in inspector.get_indexes(table_name)
            if item.get("unique")
        }
        return constraints | indexes

    event_unique_sets = unique_column_sets("moderation_events")
    if ("confirmation_id",) not in event_unique_sets:
        raise RuntimeError("moderation_events.confirmation_id unique constraint is missing")

    confirmation_unique_sets = unique_column_sets("moderation_confirmations")
    if ("token_digest",) not in confirmation_unique_sets:
        raise RuntimeError("moderation_confirmations.token_digest unique constraint is missing")

    expected_indexes = {
        "moderation_confirmations": {
            ("token_digest",), ("target_type",), ("target_id",), ("requested_by",),
            ("expires_at",), ("requested_by", "expires_at"),
        },
        "moderation_events": {
            ("confirmation_id",), ("target_type",), ("target_id",), ("actor_id",),
            ("created_at",), ("target_type", "target_id", "created_at"),
        },
    }
    expected_checks = {
        "moderation_confirmations": {
            "ck_moderation_confirmation_target_type",
            "ck_moderation_confirmation_action",
            "ck_moderation_confirmation_consumed_pair",
        },
        "moderation_events": {
            "ck_moderation_event_target_type",
            "ck_moderation_event_action",
            "ck_moderation_event_state_change",
        },
    }
    for table_name in TABLES:
        actual_indexes = {
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
        }
        missing_indexes = expected_indexes[table_name] - actual_indexes
        if missing_indexes:
            raise RuntimeError(
                f"Community moderation ledger table {table_name} lacks indexes: {sorted(missing_indexes)}"
            )
        actual_check_names = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(table_name)
        }
        missing_checks = expected_checks[table_name] - actual_check_names
        if missing_checks:
            raise RuntimeError(
                f"Community moderation ledger table {table_name} lacks checks: {sorted(missing_checks)}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    present = _table_names(bind) & TABLES
    if not present:
        _create_confirmation_table()
        _create_event_table()
        return
    if present != TABLES:
        missing = ", ".join(sorted(TABLES - present))
        raise RuntimeError(
            "Community moderation ledger is partial; refusing automatic repair. "
            f"Missing tables: {missing}"
        )
    _assert_existing_contract(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in ("moderation_events", "moderation_confirmations"):
        count = bind.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one()
        if count:
            raise RuntimeError(
                "Moderation ledger contains audit data and cannot be downgraded. Restore an approved backup instead."
            )
    op.drop_table("moderation_events")
    op.drop_table("moderation_confirmations")

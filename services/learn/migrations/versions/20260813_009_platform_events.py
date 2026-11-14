"""플랫폼 이벤트·이벤트 감사 도메인을 additive하게 추가한다.

이 마이그레이션은 CTF 대회, 점수, 커뮤니티 공지·게시물, 코스 테이블을 읽거나
변경하지 않는다. 새 테이블의 데이터가 존재하면 downgrade는 감사 기록 손실을 막기
위해 fail-closed로 중단한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_009"
down_revision: Union[str, None] = "20260813_008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EVENT_TABLE = "platform_events"
_AUDIT_TABLE = "platform_event_audit_events"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    table_names = _table_names()
    has_events = _EVENT_TABLE in table_names
    has_audits = _AUDIT_TABLE in table_names
    if has_events and has_audits:
        return
    if has_events or has_audits:
        raise RuntimeError(
            "플랫폼 이벤트 migration의 테이블 상태가 불완전합니다. "
            "검증된 백업과 스키마 상태를 확인한 뒤 수동 복구 계획을 수립하세요."
        )

    op.create_table(
        _EVENT_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("content_markdown", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("external_url", sa.String(length=2048), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visible_from_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visible_until_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archive_reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("archived_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "category IN ('workshop','seminar','study','networking','notice','other')",
            name="ck_platform_events_category",
        ),
        sa.CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_platform_events_status",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_platform_events_schedule"),
        sa.CheckConstraint(
            "visible_until_at IS NULL OR visible_until_at > visible_from_at",
            name="ck_platform_events_visibility_window",
        ),
    )
    op.create_index("ix_platform_events_category", _EVENT_TABLE, ["category"])
    op.create_index("ix_platform_events_starts_at", _EVENT_TABLE, ["starts_at"])
    op.create_index("ix_platform_events_ends_at", _EVENT_TABLE, ["ends_at"])
    op.create_index("ix_platform_events_visible_from_at", _EVENT_TABLE, ["visible_from_at"])
    op.create_index("ix_platform_events_visible_until_at", _EVENT_TABLE, ["visible_until_at"])
    op.create_index("ix_platform_events_status", _EVENT_TABLE, ["status"])
    op.create_index("ix_platform_events_created_by", _EVENT_TABLE, ["created_by"])
    op.create_index("ix_platform_events_updated_by", _EVENT_TABLE, ["updated_by"])
    op.create_index("ix_platform_events_archived_by", _EVENT_TABLE, ["archived_by"])
    op.create_index(
        "ix_platform_events_status_visible_from",
        _EVENT_TABLE,
        ["status", "visible_from_at"],
    )
    op.create_index(
        "ix_platform_events_category_starts_at",
        _EVENT_TABLE,
        ["category", "starts_at"],
    )

    op.create_table(
        _AUDIT_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_EVENT_TABLE}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_platform_event_audit_events_event_id", _AUDIT_TABLE, ["event_id"])
    op.create_index("ix_platform_event_audit_events_actor_id", _AUDIT_TABLE, ["actor_id"])
    op.create_index("ix_platform_event_audit_events_action", _AUDIT_TABLE, ["action"])
    op.create_index("ix_platform_event_audit_events_created_at", _AUDIT_TABLE, ["created_at"])
    op.create_index(
        "ix_platform_event_audit_events_event_created",
        _AUDIT_TABLE,
        ["event_id", "created_at"],
    )


def downgrade() -> None:
    table_names = _table_names()
    has_events = _EVENT_TABLE in table_names
    has_audits = _AUDIT_TABLE in table_names
    if not has_events and not has_audits:
        return
    if not has_events or not has_audits:
        raise RuntimeError(
            "플랫폼 이벤트 downgrade의 테이블 상태가 불완전합니다. "
            "자동 삭제를 중단하고 검증된 백업으로 복구하세요."
        )

    bind = op.get_bind()
    event_count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {_EVENT_TABLE}")).scalar_one()
    audit_count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {_AUDIT_TABLE}")).scalar_one()
    if event_count or audit_count:
        raise RuntimeError(
            "플랫폼 이벤트 또는 감사 데이터가 존재하므로 destructive downgrade를 중단합니다. "
            "검증된 백업·보존 계획 없이 이벤트 감사 기록을 삭제하지 마세요."
        )

    op.drop_table(_AUDIT_TABLE)
    op.drop_table(_EVENT_TABLE)

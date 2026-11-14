"""문제 컨테이너 환경 검증 revision·원장을 추가한다.

기존 공개 문제를 자동 비공개·삭제·초기화하지 않는다. 과거 환경 문제는 새 revision
필드만 갖는 legacy_unverified 상태가 되며, 새 환경 생성·대회 배정 전에 명시 검증을
요구한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_003"
down_revision: Union[str, None] = "20260813_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _add_challenge_columns_if_missing() -> None:
    columns = _column_names("challenges")
    if "environment_revision" not in columns:
        op.add_column(
            "challenges",
            sa.Column(
                "environment_revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
    if "environment_verified_revision" not in columns:
        op.add_column("challenges", sa.Column("environment_verified_revision", sa.Integer(), nullable=True))
    if "environment_verified_at" not in columns:
        op.add_column("challenges", sa.Column("environment_verified_at", sa.DateTime(timezone=True), nullable=True))
    if "environment_verified_by" not in columns:
        op.add_column(
            "challenges",
            sa.Column("environment_verified_by", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if "environment_verified_image_id" not in columns:
        op.add_column("challenges", sa.Column("environment_verified_image_id", sa.String(length=128), nullable=True))
    if "environment_last_checked_at" not in columns:
        op.add_column("challenges", sa.Column("environment_last_checked_at", sa.DateTime(timezone=True), nullable=True))
    if "environment_last_failure_code" not in columns:
        op.add_column("challenges", sa.Column("environment_last_failure_code", sa.String(length=64), nullable=True))


def _create_verification_table() -> None:
    op.create_table(
        "challenge_environment_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("image_reference", sa.String(length=500), nullable=False),
        sa.Column("image_id", sa.String(length=128), nullable=False),
        sa.Column("verified_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ready', 'failed')",
            name="ck_challenge_environment_verification_status",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_challenge_environment_verification_revision",
        ),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_challenge_environment_verifications_challenge_checked",
        "challenge_environment_verifications",
        ["challenge_id", "checked_at"],
    )


def upgrade() -> None:
    tables = _table_names()
    if "challenges" not in tables:
        # 새 DB에서는 app bootstrap이 현재 model metadata로 테이블을 생성한다.
        return

    _add_challenge_columns_if_missing()

    if "challenge_environment_verifications" not in tables:
        _create_verification_table()
        return

    expected = {
        "id", "challenge_id", "revision", "status", "failure_code", "image_reference",
        "image_id", "verified_by", "checked_at",
    }
    missing = sorted(expected - _column_names("challenge_environment_verifications"))
    if missing:
        raise RuntimeError(
            "기존 challenge_environment_verifications 테이블이 현재 검증 원장 계약과 다릅니다. "
            f"누락 컬럼: {', '.join(missing)}. 백업 후 별도 복구 migration을 적용하세요."
        )

def downgrade() -> None:
    tables = _table_names()
    if "challenge_environment_verifications" in tables:
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM challenge_environment_verifications")
        ).scalar_one()
        if count:
            raise RuntimeError(
                "환경 검증 원장 데이터가 있어 downgrade를 중단했습니다. "
                "검증된 백업 복구 절차를 사용하세요."
            )
        op.drop_table("challenge_environment_verifications")

    if "challenges" not in _table_names():
        return
    columns = _column_names("challenges")
    for name in (
        "environment_last_failure_code",
        "environment_last_checked_at",
        "environment_verified_image_id",
        "environment_verified_by",
        "environment_verified_at",
        "environment_verified_revision",
        "environment_revision",
    ):
        if name in columns:
            op.drop_column("challenges", name)

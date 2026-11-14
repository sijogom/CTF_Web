"""dynamic_instance 플래그 정책·발급 원장을 추가한다.

기존 static/regex 문제와 제출은 변경하지 않는다. dynamic_instance는 정책, 현재 환경
검증 revision/image ID, instance 소유자·대회·TTL을 모두 가진 issuance가 있을 때만
동작하며 raw flag/token은 이 migration이 만드는 어떤 컬럼에도 저장하지 않는다.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260813_005"
down_revision: Union[str, None] = "20260813_004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


POLICY_TABLE = "challenge_flag_policies"
ISSUANCE_TABLE = "dynamic_flag_issuances"
SUBMISSIONS_TABLE = "submissions"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _require_columns(table_name: str, expected: set[str]) -> None:
    missing = sorted(expected - _column_names(table_name))
    if missing:
        raise RuntimeError(
            f"기존 {table_name} 테이블이 dynamic_instance 플래그 계약과 다릅니다. "
            f"누락 컬럼: {', '.join(missing)}. 백업 후 별도 복구 migration을 적용하세요."
        )


def _create_policy_table() -> None:
    op.create_table(
        POLICY_TABLE,
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="dynamic_instance"),
        sa.Column("delivery", sa.String(length=16), nullable=False, server_default="file"),
        sa.Column("runtime_path", sa.String(length=256), nullable=False, server_default="/flag.txt"),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hmac_key_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode = 'dynamic_instance'", name="ck_challenge_flag_policy_mode"),
        sa.CheckConstraint("delivery = 'file'", name="ck_challenge_flag_policy_delivery"),
        sa.CheckConstraint("runtime_path = '/flag.txt'", name="ck_challenge_flag_policy_runtime_path"),
        sa.CheckConstraint("policy_revision >= 1", name="ck_challenge_flag_policy_revision"),
        sa.CheckConstraint("ttl_seconds BETWEEN 60 AND 86400", name="ck_challenge_flag_policy_ttl"),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("challenge_id"),
    )


def _create_issuance_table() -> None:
    op.create_table(
        ISSUANCE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_locator", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("challenge_id", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("competition_id", sa.String(length=36), nullable=True),
        sa.Column("environment_revision", sa.Integer(), nullable=False),
        sa.Column("verified_image_id", sa.String(length=128), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("hmac_key_version", sa.String(length=64), nullable=False),
        sa.Column("token_hmac", sa.LargeBinary(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=64), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("environment_revision >= 1", name="ck_dynamic_flag_issuance_environment_revision"),
        sa.CheckConstraint("policy_revision >= 1", name="ck_dynamic_flag_issuance_policy_revision"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_dynamic_flag_issuance_expiry"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_dynamic_flag_issuance_revoked_at",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= issued_at",
            name="ck_dynamic_flag_issuance_consumed_at",
        ),
        sa.CheckConstraint(
            "octet_length(token_hmac) = 32",
            name="ck_dynamic_flag_issuance_token_hmac_length",
        ),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_where=None,
) -> None:
    if index_name not in _index_names(table_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=unique,
            postgresql_where=postgresql_where,
        )


def _has_unique_key(table_name: str, columns: list[str]) -> bool:
    """기존 metadata/create_all이 만든 이름 다른 unique 제약도 안전하게 인식한다."""
    inspector = sa.inspect(op.get_bind())
    expected = tuple(columns)
    for constraint in inspector.get_unique_constraints(table_name):
        if tuple(constraint.get("column_names") or ()) == expected:
            return True
    for index in inspector.get_indexes(table_name):
        if index.get("unique") and tuple(index.get("column_names") or ()) == expected:
            return True
    return False


def _create_unique_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
) -> None:
    if not _has_unique_key(table_name, columns):
        op.create_index(index_name, table_name, columns, unique=True)


def _add_submission_link_if_missing() -> None:
    columns = _column_names(SUBMISSIONS_TABLE)
    if "dynamic_flag_issuance_id" not in columns:
        op.add_column(
            SUBMISSIONS_TABLE,
            sa.Column(
                "dynamic_flag_issuance_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(
                    f"{ISSUANCE_TABLE}.id",
                    name="fk_submissions_dynamic_flag_issuance",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
        )
    _create_unique_index_if_missing(
        SUBMISSIONS_TABLE,
        "uq_submissions_dynamic_flag_issuance",
        ["dynamic_flag_issuance_id"],
    )


def _ensure_challenge_flag_type_constraint() -> None:
    bind = op.get_bind()
    unexpected_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM challenges "
            "WHERE flag_type NOT IN ('static', 'regex', 'dynamic_instance')"
        )
    ).scalar_one()
    if unexpected_count:
        raise RuntimeError(
            "기존 challenges.flag_type에 dynamic_instance 계약 밖의 값이 있어 migration을 중단했습니다. "
            "해당 문제를 자동 변환하지 않았습니다. 백업 후 명시적인 데이터 정리 계획을 적용하세요."
        )
    op.execute("ALTER TABLE challenges DROP CONSTRAINT IF EXISTS ck_challenges_flag_type")
    op.execute(
        "ALTER TABLE challenges ADD CONSTRAINT ck_challenges_flag_type "
        "CHECK (flag_type IN ('static', 'regex', 'dynamic_instance'))"
    )


def upgrade() -> None:
    tables = _table_names()
    if "challenges" not in tables:
        # 새 DB는 app metadata가 current schema를 생성한다.
        return
    if SUBMISSIONS_TABLE not in tables:
        raise RuntimeError("submissions 테이블이 없어 dynamic_instance migration을 안전하게 적용할 수 없습니다.")

    _ensure_challenge_flag_type_constraint()

    if POLICY_TABLE not in tables:
        _create_policy_table()
    else:
        _require_columns(
            POLICY_TABLE,
            {
                "challenge_id", "mode", "delivery", "runtime_path", "ttl_seconds",
                "policy_revision", "hmac_key_version", "enabled", "created_at", "updated_at",
            },
        )

    if ISSUANCE_TABLE not in tables:
        _create_issuance_table()
    else:
        _require_columns(
            ISSUANCE_TABLE,
            {
                "id", "instance_locator", "instance_id", "challenge_id", "user_id",
                "competition_id", "environment_revision", "verified_image_id", "policy_revision",
                "hmac_key_version", "token_hmac", "issued_at", "expires_at", "revoked_at",
                "revocation_reason", "consumed_at",
            },
        )

    _create_index_if_missing(ISSUANCE_TABLE, "ix_dynamic_flag_issuances_instance_id", ["instance_id"])
    _create_index_if_missing(ISSUANCE_TABLE, "ix_dynamic_flag_issuances_challenge_id", ["challenge_id"])
    _create_index_if_missing(ISSUANCE_TABLE, "ix_dynamic_flag_issuances_user_id", ["user_id"])
    _create_index_if_missing(ISSUANCE_TABLE, "ix_dynamic_flag_issuances_competition_id", ["competition_id"])
    _create_unique_index_if_missing(
        ISSUANCE_TABLE,
        "uq_dynamic_flag_issuance_locator",
        ["instance_locator"],
    )
    _create_index_if_missing(
        ISSUANCE_TABLE,
        "ix_dynamic_flag_issuances_instance_active",
        ["instance_id"],
        unique=True,
        # 만료 시각은 partial index predicate에 넣을 수 없다. issue 경로가 만료 row를
        # 명시 revoke한 뒤에 새 발급을 넣어 slot을 해제한다.
        postgresql_where=sa.text("revoked_at IS NULL AND consumed_at IS NULL"),
    )
    _create_index_if_missing(
        ISSUANCE_TABLE,
        "ix_dynamic_flag_issuances_challenge_owner_context_expiry",
        ["challenge_id", "user_id", "competition_id", "expires_at"],
    )
    _add_submission_link_if_missing()


def downgrade() -> None:
    tables = _table_names()
    protected_tables = [table for table in (POLICY_TABLE, ISSUANCE_TABLE) if table in tables]
    for table_name in protected_tables:
        count = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        if count:
            raise RuntimeError(
                "dynamic_instance 정책 또는 발급 원장 데이터가 있어 안전하게 downgrade할 수 없습니다. "
                "검증된 백업·만료·정리 절차를 사용하세요."
            )

    if "challenges" in tables:
        dynamic_challenge_count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM challenges WHERE flag_type = 'dynamic_instance'")
        ).scalar_one()
        if dynamic_challenge_count:
            raise RuntimeError(
                "dynamic_instance 문제 행이 있어 안전하게 downgrade할 수 없습니다. "
                "문제를 static 또는 regex로 명시적으로 전환하거나 검증된 복구 절차를 사용하세요."
            )

    if (
        SUBMISSIONS_TABLE in tables
        and "dynamic_flag_issuance_id" in _column_names(SUBMISSIONS_TABLE)
        and "uq_submissions_dynamic_flag_issuance" in _index_names(SUBMISSIONS_TABLE)
    ):
        op.drop_index("uq_submissions_dynamic_flag_issuance", table_name=SUBMISSIONS_TABLE)
    if SUBMISSIONS_TABLE in tables and "dynamic_flag_issuance_id" in _column_names(SUBMISSIONS_TABLE):
        op.drop_column(SUBMISSIONS_TABLE, "dynamic_flag_issuance_id")
    if ISSUANCE_TABLE in tables:
        op.drop_table(ISSUANCE_TABLE)
    if POLICY_TABLE in tables:
        op.drop_table(POLICY_TABLE)
    if "challenges" in tables:
        op.execute("ALTER TABLE challenges DROP CONSTRAINT IF EXISTS ck_challenges_flag_type")
        op.execute(
            "ALTER TABLE challenges ADD CONSTRAINT ck_challenges_flag_type "
            "CHECK (flag_type IN ('static', 'regex'))"
        )

"""실패한 컨테이너 검증 원장에 image_id를 선택값으로 허용한다.

준비 완료 레코드는 API 계약에서 계속 Docker image ID를 요구한다. 다만 이미지 생성 전
실패, Docker inspect 실패처럼 image ID를 얻을 수 없는 실패도 감사 원장에 남겨야 하므로
실패 레코드에 한해 NULL을 허용한다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_004"
down_revision: Union[str, None] = "20260813_003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "challenge_environment_verifications"
COLUMN_NAME = "image_id"


def _image_id_column() -> dict | None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return None
    return next(
        (
            column
            for column in inspector.get_columns(TABLE_NAME)
            if column["name"] == COLUMN_NAME
        ),
        None,
    )


def upgrade() -> None:
    table_names = set(sa.inspect(op.get_bind()).get_table_names())
    # 000은 기존 create_all 기반 Core 스키마의 기준선일 뿐 새 DB에 Core 테이블을
    # 만들지 않는다. 완전히 빈 DB에서는 003도 의도적으로 넘기며, 앱 시작 단계의
    # Base.metadata.create_all이 현재 모델(이미 image_id nullable)을 만든다.
    # 반대로 둘 중 하나라도 존재하면 부분/손상 상태이므로 자동 추측하지 않는다.
    if "challenges" not in table_names and TABLE_NAME not in table_names:
        return

    column = _image_id_column()
    if column is None:
        raise RuntimeError(
            "컨테이너 환경 검증 원장의 image_id 컬럼이 없습니다. "
            "20260813_003 migration 적용 상태를 먼저 복구하세요."
        )
    if not column.get("nullable", False):
        op.alter_column(
            TABLE_NAME,
            COLUMN_NAME,
            existing_type=sa.String(length=128),
            nullable=True,
        )


def downgrade() -> None:
    column = _image_id_column()
    if column is None or not column.get("nullable", False):
        return

    null_count = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE {COLUMN_NAME} IS NULL")
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "image_id가 없는 실패 검증 원장 레코드가 있어 안전하게 downgrade할 수 없습니다. "
            "검증된 백업·복구 절차를 사용하세요."
        )

    op.alter_column(
        TABLE_NAME,
        COLUMN_NAME,
        existing_type=sa.String(length=128),
        nullable=False,
    )

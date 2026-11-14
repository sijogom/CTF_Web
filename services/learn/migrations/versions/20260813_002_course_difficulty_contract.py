"""코스 난이도를 교육 콘텐츠 4단계로 안전하게 정규화.

기존 워게임 난이도 값은 정보를 완전히 역변환할 수 없으므로 이 migration의 downgrade는
의도적으로 중단한다. 실제 운영 적용 전에는 백업과 현재 값 분포 사전점검이 필요하다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260813_002"
down_revision: Union[str, None] = "20260813_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_VALUES = (
    "beginner", "easy", "medium_low", "medium", "medium_high", "hard", "expert", "impossible",
)
_TARGET_VALUES = ("introductory", "basic", "intermediate", "advanced")
_ALL_ALLOWED_VALUES_SQL = ", ".join(f"'{value}'" for value in (*_LEGACY_VALUES, *_TARGET_VALUES))
_TARGET_VALUES_SQL = ", ".join(f"'{value}'" for value in _TARGET_VALUES)


def upgrade() -> None:
    bind = op.get_bind()
    if "courses" not in sa.inspect(bind).get_table_names():
        # 새 DB에서는 앱 bootstrap이 현재 모델의 CHECK로 courses를 생성한다.
        return

    unexpected_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            f"SELECT DISTINCT difficulty FROM courses WHERE difficulty NOT IN ({_ALL_ALLOWED_VALUES_SQL})"
            ") AS unexpected_difficulties"
        )
    ).scalar_one()
    if unexpected_count:
        raise RuntimeError(
            "courses.difficulty에 알려지지 않은 값이 있어 자동 난이도 정규화를 중단했습니다. "
            "백업 후 값 분포를 확인하고 별도 mapping migration을 작성하세요."
        )

    # 기존 CHECK가 새 값 입력을 막을 수 있으므로, 먼저 구·신 값 모두를 허용하고
    # 명시 매핑을 적용한 뒤 최종 4단계 CHECK로 좁힌다.
    op.execute("ALTER TABLE courses DROP CONSTRAINT IF EXISTS ck_courses_difficulty")
    op.execute(
        "ALTER TABLE courses ADD CONSTRAINT ck_courses_difficulty "
        f"CHECK (difficulty IN ({_ALL_ALLOWED_VALUES_SQL}))"
    )
    op.execute(
        "UPDATE courses SET difficulty = CASE difficulty "
        "WHEN 'beginner' THEN 'introductory' "
        "WHEN 'easy' THEN 'basic' "
        "WHEN 'medium_low' THEN 'intermediate' "
        "WHEN 'medium' THEN 'intermediate' "
        "WHEN 'medium_high' THEN 'intermediate' "
        "WHEN 'hard' THEN 'advanced' "
        "WHEN 'expert' THEN 'advanced' "
        "WHEN 'impossible' THEN 'advanced' "
        "ELSE difficulty END"
    )
    op.execute("ALTER TABLE courses DROP CONSTRAINT ck_courses_difficulty")
    op.execute(
        "ALTER TABLE courses ADD CONSTRAINT ck_courses_difficulty "
        f"CHECK (difficulty IN ({_TARGET_VALUES_SQL}))"
    )


def downgrade() -> None:
    raise RuntimeError(
        "코스 난이도 4단계 정규화는 기존 값으로 완전히 역변환할 수 없습니다. "
        "검증된 백업 복구 또는 명시적 복구 mapping을 사용하세요."
    )

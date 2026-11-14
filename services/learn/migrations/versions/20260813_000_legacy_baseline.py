"""기존 Learn 스키마의 명시적 기준선.

이 revision은 이미 ``create_all``로 운영되던 핵심 테이블을 재작성하지 않는다.
기존 DB의 baseline 기록은 사전점검 및 백업이 끝난 경우에만 허용한다.
"""
from typing import Sequence, Union


revision: str = "20260813_000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 기존 핵심 테이블은 이 revision에서 생성·변형하지 않는다.
    pass


def downgrade() -> None:
    # 기준선은 데이터 정의를 수행하지 않았으므로 되돌릴 작업도 없다.
    pass

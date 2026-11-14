"""지식맵 V2에 공개 이벤트·교육 과제 노드 유형을 추가한다.

이 migration은 기존 관계·원본 콘텐츠를 변환하지 않는다. 노드 레지스트리의 CHECK
어휘만 확장하고, 공개 여부는 요청 시 이벤트 공개 기간과 과제 수강 권한을 다시
확인한다. 따라서 event/assignment 노드는 승인된 관계가 실제로 연결될 때만
projection outbox를 통해 생성된다.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260814_014"
down_revision: Union[str, None] = "20260814_013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "knowledge_content_nodes"
_CHECK = "ck_knowledge_content_node_type"
_OLD_TYPES = "'challenge', 'course', 'lesson', 'knowledge_tag', 'hint'"
_NEW_TYPES = f"{_OLD_TYPES}, 'event', 'assignment'"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _checks() -> set[str]:
    return {
        item["name"]
        for item in _inspector().get_check_constraints(_TABLE)
        if item.get("name")
    }


def upgrade() -> None:
    if _TABLE not in set(_inspector().get_table_names()):
        raise RuntimeError(
            "knowledge_content_nodes가 없어 이벤트·과제 노드 어휘 migration을 적용하지 않습니다."
        )
    checks = _checks()
    if _CHECK in checks:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        f"entity_type IN ({_NEW_TYPES})",
    )


def downgrade() -> None:
    if _TABLE not in set(_inspector().get_table_names()):
        raise RuntimeError("knowledge_content_nodes가 없어 이벤트·과제 노드 migration을 되돌릴 수 없습니다.")
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} "
            "WHERE entity_type IN ('event', 'assignment')"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            "이벤트·과제 노드가 남아 있어 destructive downgrade를 중단합니다."
        )
    checks = _checks()
    if _CHECK in checks:
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        f"entity_type IN ({_OLD_TYPES})",
    )

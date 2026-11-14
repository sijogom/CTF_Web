"""관계 편집기의 관리자 전용 콘텐츠 검색. 공개 검색 범위는 변경하지 않는다."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, require_role
from app.database import get_db
from app.models.challenge import Challenge, Hint
from app.models.course import Course, Lesson
from app.models.education import EducationAssignment, TeachingClass
from app.models.event import PlatformEvent
from app.models.knowledge import KnowledgeTag
from app.schemas.knowledge import KnowledgeContentSearchResponse, KnowledgeContentSummary, KnowledgeContentType
from app.utils.knowledge_relations import normalize_content_identifier, resolve_content


router = APIRouter(prefix="/admin", tags=["지식 관계 관리"])


def content_search_query(entity_type: str, query: str):
    """원본 테이블의 ID·제목만 검색한다. 힌트 본문·비밀값은 검색/반환하지 않는다."""
    if entity_type == "hint":
        name = Challenge.name + literal(" · 힌트 ") + cast(Hint.order_index + 1, String)
        statement = select(Hint.id.label("id"), name.label("name")).join(Challenge, Challenge.id == Hint.challenge_id)
        identifier = Hint.id
    else:
        model, name = {
            "challenge": (Challenge, Challenge.name), "course": (Course, Course.title),
            "lesson": (Lesson, Lesson.title), "knowledge_tag": (KnowledgeTag, KnowledgeTag.name),
            "event": (PlatformEvent, PlatformEvent.title), "assignment": (EducationAssignment, EducationAssignment.title),
        }[entity_type]
        identifier = model.id
        statement = select(identifier.label("id"), name.label("name"))
        # resolver에서 부모가 있어야 해석되는 원본만 같은 기준으로 선택한다.
        if entity_type == "lesson":
            statement = statement.join(Course, Course.id == Lesson.course_id)
        elif entity_type == "assignment":
            statement = statement.join(TeachingClass, TeachingClass.id == EducationAssignment.class_id)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        matches = [name.ilike(pattern, escape="\\"), cast(identifier, String).ilike(pattern, escape="\\")]
        try:
            normalized = normalize_content_identifier(entity_type, query)
        except ValueError:
            pass
        else:
            exact_id = int(normalized) if entity_type in {"challenge", "knowledge_tag", "hint"} else UUID(normalized)
            if isinstance(exact_id, UUID) or exact_id.bit_length() <= 31:
                matches.append(identifier == exact_id)
        statement = statement.where(or_(*matches))
    return statement.order_by(func.lower(name), identifier)


@router.get("/knowledge-contents", response_model=KnowledgeContentSearchResponse)
async def search_knowledge_contents(
    type: KnowledgeContentType = Query(...),
    q: str = Query("", max_length=200),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    viewer_id: UUID = Depends(get_current_user_id),
    _=Depends(require_role("admin", "moderator")),
):
    query = q.strip()
    if any(ord(character) < 32 for character in query):
        raise HTTPException(422, "검색어에 제어 문자를 사용할 수 없습니다.")
    statement = content_search_query(type, query)
    total = await db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = (await db.execute(statement.offset(offset).limit(limit))).all()
    items = []
    for row in rows:
        content = await resolve_content(db, type, str(row.id), public_only=False, viewer_id=viewer_id)
        # count/page 조회 중 삭제된 원본을 임의 내용이나 stale registry로 대체하지 않는다.
        if content is not None:
            items.append(KnowledgeContentSummary(type=content.entity_type, id=content.entity_id,
                name=content.display_name, is_active=content.is_active, href=content.href, course_id=content.course_id))
    return KnowledgeContentSearchResponse(items=items, total=int(total or 0), offset=offset, limit=limit)

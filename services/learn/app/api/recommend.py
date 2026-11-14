"""승인된 지식 관계를 사용하는 학습 추천 API."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.api.deps import get_current_user_id
from app.api.knowledge_graph import _v2_graph_revision
from app.database import get_db
from app.models.challenge import Challenge, ChallengePrerequisite, Submission
from app.models.course import ContentStatus, Course, Lesson, UserProgress
from app.models.knowledge import (
    KnowledgeContentNode,
    KnowledgeRelation,
    RecommendationConsent,
    RecommendationConsentEvent,
    ChallengeLearningPrerequisite,
)
from app.schemas.course import CourseSummary
from app.schemas.knowledge import (
    RecommendationConsentResponse,
    RecommendationConsentUpdate,
    RecommendationEvidence,
    RecommendationItem,
    RecommendationResponse,
    RecommendationAction,
)
from app.utils.knowledge_mapping_v2 import public_evidence_summary
from app.services.knowledge_graph_pages import public_nodes_query
from app.utils.course_prerequisites import get_course_prerequisite_status
from app.utils.challenge_access import user_can_access_challenge_for_instance

router = APIRouter(tags=["recommendations"])

RECOMMENDATION_SCOPE = "learning_recommendations"
RECOMMENDATION_POLICY_VERSION = "learning-recommendations-v1"
RECOMMENDATION_RELATION_TYPES = ("prepares_for", "recommends", "helps")


def recommendation_score(strength: float, confidence: float) -> float:
    """관계 강도와 검토 신뢰도를 설명 가능한 단일 점수로 결합한다."""
    return round(max(0.0, min(1.0, float(strength) * float(confidence))), 6)


def _consent_response(consent: RecommendationConsent | None) -> RecommendationConsentResponse:
    if consent is None:
        return RecommendationConsentResponse(
            enabled=False,
            policy_version=RECOMMENDATION_POLICY_VERSION,
        )
    return RecommendationConsentResponse(
        enabled=bool(consent.enabled),
        policy_version=consent.policy_version,
        granted_at=consent.granted_at,
        withdrawn_at=consent.withdrawn_at,
    )


def _consent_lock_key(user_id: UUID) -> int:
    """동의 row가 아직 없을 때도 동일 사용자 PUT을 직렬화할 PostgreSQL key."""
    return user_id.int % (2**63 - 1)


async def _load_consent(
    db: AsyncSession,
    user_id: UUID,
    *,
    lock: bool = False,
) -> RecommendationConsent | None:
    query = select(RecommendationConsent).where(
        RecommendationConsent.user_id == user_id,
        RecommendationConsent.scope == RECOMMENDATION_SCOPE,
    )
    if lock:
        query = query.with_for_update()
    result = await db.execute(query.execution_options(populate_existing=True))
    return result.scalar_one_or_none()


@router.get("/me/recommendation-consent", response_model=RecommendationConsentResponse)
async def get_recommendation_consent(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> RecommendationConsentResponse:
    """개인화 추천 사용 동의 상태를 반환한다. 기본값은 비활성이다."""
    try:
        return _consent_response(await _load_consent(db, user_id))
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "추천 동의 상태를 확인할 수 없습니다.",
        ) from exc


@router.put("/me/recommendation-consent", response_model=RecommendationConsentResponse)
async def update_recommendation_consent(
    payload: RecommendationConsentUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> RecommendationConsentResponse:
    """동의를 원자적으로 변경하고 append-only 이력을 남긴다."""
    now = datetime.now(timezone.utc)
    try:
        await db.execute(select(func.pg_advisory_xact_lock(_consent_lock_key(user_id))))
        consent = await _load_consent(db, user_id, lock=True)
        if consent is None:
            consent = RecommendationConsent(
                user_id=user_id,
                scope=RECOMMENDATION_SCOPE,
                policy_version=RECOMMENDATION_POLICY_VERSION,
                enabled=payload.enabled,
                granted_at=now if payload.enabled else None,
                withdrawn_at=None if payload.enabled else now,
            )
            db.add(consent)
            await db.flush()
        else:
            consent.policy_version = RECOMMENDATION_POLICY_VERSION
            consent.enabled = payload.enabled
            if payload.enabled:
                consent.granted_at = now
                consent.withdrawn_at = None
            else:
                consent.withdrawn_at = now

        db.add(
            RecommendationConsentEvent(
                consent_id=consent.id,
                user_id=user_id,
                action="enabled" if payload.enabled else "disabled",
                policy_version=RECOMMENDATION_POLICY_VERSION,
            )
        )
        await db.commit()
        await db.refresh(consent)
        return _consent_response(consent)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "추천 동의 상태를 저장할 수 없습니다.",
        ) from exc


async def _personalization_seeds(
    db: AsyncSession,
    user_id: UUID,
) -> tuple[set[str], set[str], set[str]]:
    completed_lessons = await db.scalars(
        select(UserProgress.lesson_id).where(
            UserProgress.user_id == user_id,
            UserProgress.completed.is_(True),
        )
    )
    solved_challenges = await db.scalars(
        select(Submission.challenge_id).where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.competition_id.is_(None),
        )
    )
    course_rows = await db.execute(
        select(
            Lesson.course_id,
            func.count(Lesson.id).label("total_lessons"),
            func.count(UserProgress.id).label("completed_lessons"),
        )
        .outerjoin(
            UserProgress,
            and_(
                UserProgress.lesson_id == Lesson.id,
                UserProgress.user_id == user_id,
                UserProgress.completed.is_(True),
            ),
        )
        .group_by(Lesson.course_id)
    )
    completed_courses = {
        str(course_id)
        for course_id, total_lessons, completed_count in course_rows.all()
        if total_lessons > 0 and completed_count >= total_lessons
    }
    return (
        {str(item) for item in completed_lessons},
        {str(item) for item in solved_challenges},
        completed_courses,
    )


@router.get("/me/recommendations", response_model=RecommendationResponse)
async def get_personalized_recommendations(
    limit: int = Query(10, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> RecommendationResponse:
    """승인된 공개 관계와 사용자의 완료 이력을 교차해 설명 가능한 추천을 반환한다."""
    generated_at = datetime.now(timezone.utc)
    try:
        consent = await _load_consent(db, user_id)
        if consent is None or not consent.enabled:
            return RecommendationResponse(
                consent_required=True,
                enabled=False,
                policy_version=consent.policy_version if consent else RECOMMENDATION_POLICY_VERSION,
                generated_at=generated_at,
            )

        completed_lessons, solved_challenges, completed_courses = await _personalization_seeds(db, user_id)
        graph_revision = await _v2_graph_revision(db)
        if not completed_lessons and not solved_challenges and not completed_courses:
            return RecommendationResponse(
                consent_required=False,
                enabled=True,
                policy_version=consent.policy_version,
                graph_revision=graph_revision,
                generated_at=generated_at,
                empty_reason="no_history",
            )

        source_node = aliased(KnowledgeContentNode)
        target_node = aliased(KnowledgeContentNode)
        public_nodes = public_nodes_query(user_id)
        source_public, target_public = public_nodes.alias("recommend_source"), public_nodes.alias("recommend_target")
        source_filter = or_(
            and_(source_node.entity_type == "lesson", source_node.entity_id.in_(completed_lessons)),
            and_(source_node.entity_type == "challenge", source_node.entity_id.in_(solved_challenges)),
            and_(source_node.entity_type == "course", source_node.entity_id.in_(completed_courses)),
        )
        target_filter = or_(
            and_(target_node.entity_type == "lesson", target_node.entity_id.not_in(completed_lessons)),
            and_(target_node.entity_type == "challenge", target_node.entity_id.not_in(solved_challenges)),
            and_(target_node.entity_type == "course", target_node.entity_id.not_in(completed_courses)),
        )
        score = KnowledgeRelation.strength * KnowledgeRelation.confidence
        eligible = (select(
            KnowledgeRelation.id.label("relation_id"), target_node.id.label("target_node_id"),
            score.label("score"),
            func.row_number().over(partition_by=target_node.id,
                order_by=(score.desc(), KnowledgeRelation.id)).label("evidence_rank"),
            source_public.c.type.label("source_type"), source_public.c.entity_id.label("source_id"),
            source_public.c.name.label("source_name"), target_public.c.type.label("target_type"),
            target_public.c.entity_id.label("target_id"), target_public.c.name.label("target_name"),
            target_public.c.href.label("target_href"), KnowledgeRelation.relation_type,
            KnowledgeRelation.strength, KnowledgeRelation.confidence, KnowledgeRelation.evidence_summary,
        ).select_from(KnowledgeRelation)
            .join(source_node, source_node.id == KnowledgeRelation.source_node_id)
            .join(target_node, target_node.id == KnowledgeRelation.target_node_id)
            .join(source_public, source_public.c.key == source_node.entity_type + ":" + source_node.entity_id)
            .join(target_public, target_public.c.key == target_node.entity_type + ":" + target_node.entity_id)
            .where(KnowledgeRelation.status == "approved", KnowledgeRelation.relation_type.in_(RECOMMENDATION_RELATION_TYPES),
                   source_filter, target_filter)).cte("eligible_recommendations")
        best_targets = (select(eligible.c.target_node_id).where(eligible.c.evidence_rank == 1)
            .order_by(eligible.c.score.desc(), eligible.c.target_node_id).limit(limit))
        rows = (await db.execute(select(eligible).where(eligible.c.target_node_id.in_(best_targets),
            eligible.c.evidence_rank <= 3).order_by(eligible.c.score.desc(), eligible.c.relation_id))).mappings().all()
        recommendations = {}
        for row in rows:
            key = (row["target_type"], row["target_id"])
            if key not in recommendations:
                recommendations[key] = RecommendationItem(type=row["target_type"], id=row["target_id"],
                    name=row["target_name"], href=row["target_href"],
                    score=recommendation_score(row["strength"], row["confidence"]))
            item = recommendations[key]
            if row["relation_type"] not in item.reason_codes:
                item.reason_codes.append(row["relation_type"])
            item.evidence.append(RecommendationEvidence(source_type=row["source_type"], source_id=row["source_id"],
                source_name=row["source_name"], relation_type=row["relation_type"], strength=float(row["strength"]),
                confidence=float(row["confidence"]), evidence_summary=public_evidence_summary(row["evidence_summary"])))
        items = sorted(recommendations.values(), key=lambda item: (-item.score, item.name, item.id))
        for item in items:
            item.is_locked, item.prerequisites = await _recommendation_prerequisites(db, user_id, item,
                completed_lessons, solved_challenges, completed_courses)
            item.next_action = next((entry for entry in item.prerequisites if not entry.completed), None) if item.is_locked else None
            if not item.is_locked:
                item.next_action = RecommendationAction(type=item.type, id=item.id, name=item.name, href=item.href)
        consent = await _load_consent(db, user_id)
        if consent is None or not consent.enabled:
            return RecommendationResponse(consent_required=True, enabled=False,
                policy_version=RECOMMENDATION_POLICY_VERSION, generated_at=generated_at)
        if await _v2_graph_revision(db) != graph_revision:
            raise HTTPException(409, "추천의 지식 그래프가 변경되었습니다. 다시 조회하세요.")
        return RecommendationResponse(consent_required=False, enabled=True, policy_version=consent.policy_version,
            graph_revision=graph_revision, generated_at=generated_at, items=items,
            empty_reason=None if items else "no_candidates")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "추천을 계산할 수 없습니다.",
        ) from exc


async def _recommendation_prerequisites(db, user_id, item, completed_lessons, solved_challenges, completed_courses):
    from app.utils.knowledge_relations import resolve_content
    prerequisites = []
    if item.type in {"course", "lesson"}:
        course_id = UUID(item.id)
        if item.type == "lesson":
            course_id = await db.scalar(select(Lesson.course_id).where(Lesson.id == UUID(item.id)))
        locked, details = await get_course_prerequisite_status(db, course_id=course_id, user_id=user_id)
        prerequisites = [RecommendationAction(type="course", id=detail["id"], name=detail["title"],
            href=f"/learn/{detail['id']}", completed=detail["completed"]) for detail in details]
        return locked, prerequisites
    challenge_id = int(item.id)
    required_ids = (await db.scalars(select(ChallengePrerequisite.prerequisite_id)
        .join(Challenge, Challenge.id == ChallengePrerequisite.prerequisite_id)
        .where(ChallengePrerequisite.challenge_id == challenge_id, Challenge.is_published.is_(True),
               Challenge.challenge_type == "wargame"))).all()
    required = [("challenge", str(identifier)) for identifier in required_ids]
    required.extend((row.prerequisite_type, str(row.prerequisite_target_id)) for row in
        (await db.scalars(select(ChallengeLearningPrerequisite).where(ChallengeLearningPrerequisite.challenge_id == challenge_id))).all())
    completed_by_type = {"course": completed_courses, "lesson": completed_lessons, "challenge": solved_challenges}
    for kind, identifier in required:
        content = await resolve_content(db, kind, identifier, public_only=True, viewer_id=user_id)
        if content and content.href:
            prerequisites.append(RecommendationAction(type=kind, id=identifier, name=content.display_name,
                href=content.href, completed=identifier in completed_by_type[kind]))
    locked = not await user_can_access_challenge_for_instance(challenge_id=challenge_id, user_id=user_id,
                                                             competition_id=None, db=db)
    return locked, prerequisites


async def _resolve_public_content(
    db: AsyncSession,
    node: KnowledgeContentNode,
) -> tuple[str, str, str, str] | None:
    """Resolver를 통해 공개 원본만 추천 응답에 허용한다."""
    from app.utils.knowledge_relations import resolve_content

    content = await resolve_content(
        db,
        node.entity_type,
        node.entity_id,
        public_only=True,
    )
    if content is None or not content.is_active or not content.href:
        return None
    if content.entity_type not in {"course", "lesson", "challenge"}:
        return None
    return content.entity_type, content.entity_id, content.display_name, content.href


@router.get("/recommend", response_model=list[CourseSummary])
async def recommend_courses(
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """기존 코스 추천 호환 API. 개인화 V1은 ``/me/recommendations``를 사용한다."""
    total_lessons_subq = (
        select(Lesson.course_id, func.count(Lesson.id).label("total"))
        .group_by(Lesson.course_id)
        .subquery()
    )
    completed_lessons_subq = (
        select(Lesson.course_id, func.count(UserProgress.id).label("done"))
        .join(UserProgress, UserProgress.lesson_id == Lesson.id)
        .where(UserProgress.user_id == user_id, UserProgress.completed.is_(True))
        .group_by(Lesson.course_id)
        .subquery()
    )
    completed_courses_subq = (
        select(total_lessons_subq.c.course_id)
        .join(
            completed_lessons_subq,
            total_lessons_subq.c.course_id == completed_lessons_subq.c.course_id,
        )
        .where(completed_lessons_subq.c.done >= total_lessons_subq.c.total)
        .subquery()
    )
    query = (
        select(Course)
        .where(Course.is_published.is_(True))
        .where(Course.is_deleted.is_(False))
        .where(Course.status == ContentStatus.APPROVED.value)
        .where(~Course.id.in_(select(completed_courses_subq.c.course_id)))
        .options(selectinload(Course.lessons))
        .order_by(
            case(
                (Course.difficulty == "introductory", 0),
                (Course.difficulty == "basic", 1),
                (Course.difficulty == "intermediate", 2),
                (Course.difficulty == "advanced", 3),
                else_=4,
            ),
            Course.order_index,
        )
        .limit(limit)
    )
    result = await db.execute(query)
    courses = result.scalars().all()
    return [
        CourseSummary(
            id=c.id,
            title=c.title,
            category=c.category,
            difficulty=c.difficulty,
            estimated_hours=c.estimated_hours,
            thumbnail_url=c.thumbnail_url,
            is_published=c.is_published,
            lesson_count=len(c.lessons),
            created_at=c.created_at,
        )
        for c in courses
    ]

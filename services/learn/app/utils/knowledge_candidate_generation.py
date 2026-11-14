"""지식 관계 규칙 후보 생성의 안전한 입력 수집과 프로필 저장 보조 로직.

이 모듈은 공개 가능한 제목·분류·난이도·관리자 승인 태그만 수집한다. 문제/레슨 본문,
플래그, 힌트 본문, 첨부파일, 제출 및 사용자 데이터에는 접근하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.course import ContentStatus, Course, Lesson
from app.models.education import EducationAssignment, EducationResourceLink, TeachingClass
from app.models.knowledge import (
    ChallengeKnowledgeTag,
    CourseKnowledgeTag,
    KnowledgeContentNode,
    KnowledgeContentProfile,
    KnowledgeTag,
    LessonKnowledgeTag,
)
from app.utils.knowledge_mapping_v2 import (
    SAFE_PROFILE_SCHEMA_VERSION,
    DeterministicTagPreparesForCandidate,
    deterministic_assignment_resource_helps_candidate,
    digest_json,
    normalize_allowed_profile,
    normalize_metadata_tag_slugs,
)
from app.utils.knowledge_relations import (
    ResolvedKnowledgeContent,
    ensure_content_node,
    public_tag_mapping_clause,
    public_knowledge_tag_ids_query,
    resolve_content,
)


# 후보 실행 원장의 입력 형식은 알고리즘 버전과 별도로 식별한다. 기존 실행 행은
# migration에서 ``legacy-unversioned``으로 보존하고, 이 버전부터만 profile과 node
# 투영 상태를 함께 기록한다.
CANDIDATE_INPUT_SNAPSHOT_SCHEMA_VERSION = "candidate-input-profile-node-v2"


@dataclass(frozen=True)
class CandidateContentInput:
    """규칙 엔진으로 전달할 공개 콘텐츠의 안전한 프로필 스냅샷."""

    content: ResolvedKnowledgeContent
    profile: dict[str, Any]
    profile_digest: str


@dataclass(frozen=True)
class CandidateInputCollection:
    """수집된 공개 입력과 안전 프로필 검증에서 제외된 항목 수."""

    sources: list[CandidateContentInput]
    targets: list[CandidateContentInput]
    error_count: int
    explicit_inputs: list[CandidateContentInput] = field(default_factory=list)
    explicit_candidates: list[DeterministicTagPreparesForCandidate] = field(default_factory=list)
    tag_inputs: list[CandidateContentInput] = field(default_factory=list)


def _candidate_profile(
    *,
    content: ResolvedKnowledgeContent,
    category: str | None,
    difficulty: str | None,
    tag_slugs: Iterable[str],
) -> CandidateContentInput:
    profile: dict[str, Any] = {
        "entity_type": content.entity_type,
        "entity_id": content.entity_id,
        "title": content.display_name,
        "approved_tag_slugs": sorted(set(tag_slugs)),
    }
    if category:
        profile["category"] = category
    if difficulty:
        profile["difficulty"] = difficulty
    normalized = normalize_allowed_profile(profile)
    return CandidateContentInput(
        content=content,
        profile=normalized,
        profile_digest=digest_json(normalized),
    )


async def _challenge_tag_map(db: AsyncSession, challenge_ids: list[int]) -> dict[int, set[str]]:
    if not challenge_ids:
        return {}
    result = await db.execute(
        select(ChallengeKnowledgeTag.challenge_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == ChallengeKnowledgeTag.tag_id)
        .where(
            ChallengeKnowledgeTag.challenge_id.in_(challenge_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(ChallengeKnowledgeTag),
        )
    )
    tags: dict[int, set[str]] = {}
    for challenge_id, slug in result.all():
        tags.setdefault(challenge_id, set()).add(slug)
    return tags


async def _course_tag_map(db: AsyncSession, course_ids: list[UUID]) -> dict[UUID, set[str]]:
    if not course_ids:
        return {}
    result = await db.execute(
        select(CourseKnowledgeTag.course_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == CourseKnowledgeTag.tag_id)
        .where(
            CourseKnowledgeTag.course_id.in_(course_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(CourseKnowledgeTag),
        )
    )
    tags: dict[UUID, set[str]] = {}
    for course_id, slug in result.all():
        tags.setdefault(course_id, set()).add(slug)
    return tags


async def _lesson_tag_map(db: AsyncSession, lesson_ids: list[UUID]) -> dict[UUID, set[str]]:
    if not lesson_ids:
        return {}
    result = await db.execute(
        select(LessonKnowledgeTag.lesson_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == LessonKnowledgeTag.tag_id)
        .where(
            LessonKnowledgeTag.lesson_id.in_(lesson_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(LessonKnowledgeTag),
        )
    )
    tags: dict[UUID, set[str]] = {}
    for lesson_id, slug in result.all():
        tags.setdefault(lesson_id, set()).add(slug)
    return tags


async def collect_public_tag_candidate_inputs(
    db: AsyncSession,
) -> CandidateInputCollection:
    """공개·승인 교육 콘텐츠와 공개 워게임 문제만 안전한 입력으로 수집한다.

    source는 course/lesson, target은 wargame challenge다. competition, draft,
    soft-deleted, 미승인 콘텐츠는 데이터베이스에서 처음부터 제외한다.
    """
    challenge_result = await db.execute(
        select(Challenge)
        .where(
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
        .order_by(Challenge.id)
    )
    challenges = challenge_result.scalars().all()
    course_result = await db.execute(
        select(Course)
        .where(
            Course.is_deleted.is_(False),
            Course.is_published.is_(True),
            Course.status == ContentStatus.APPROVED.value,
        )
        .order_by(Course.id)
    )
    courses = course_result.scalars().all()
    lesson_result = await db.execute(
        select(Lesson, Course)
        .join(Course, Course.id == Lesson.course_id)
        .where(
            Course.is_deleted.is_(False),
            Course.is_published.is_(True),
            Course.status == ContentStatus.APPROVED.value,
        )
        .order_by(Lesson.id)
    )
    lessons = lesson_result.all()

    challenge_tags = await _challenge_tag_map(db, [challenge.id for challenge in challenges])
    course_tags = await _course_tag_map(db, [course.id for course in courses])
    lesson_tags = await _lesson_tag_map(db, [lesson.id for lesson, _course in lessons])

    sources: list[CandidateContentInput] = []
    error_count = 0
    for course in courses:
        content = ResolvedKnowledgeContent(
            entity_type="course",
            entity_id=str(course.id),
            display_name=course.title,
            is_active=True,
            href=f"/learn/{course.id}",
            source_updated_at=course.updated_at,
        )
        try:
            sources.append(
                _candidate_profile(
                    content=content,
                    category=course.category,
                    difficulty=course.difficulty,
                    tag_slugs=(
                        course_tags.get(course.id, set())
                        | set(
                            normalize_metadata_tag_slugs(
                                category=course.category,
                                difficulty=course.difficulty,
                            )
                        )
                    ),
                )
            )
        except ValueError:
            error_count += 1
    for lesson, course in lessons:
        content = ResolvedKnowledgeContent(
            entity_type="lesson",
            entity_id=str(lesson.id),
            display_name=lesson.title,
            is_active=True,
            href=f"/learn/{course.id}/{lesson.id}",
            course_id=str(course.id),
            source_updated_at=lesson.updated_at,
        )
        # A lesson inherits its approved course tags in addition to direct lesson tags.
        tags = (
            lesson_tags.get(lesson.id, set())
            | course_tags.get(course.id, set())
            | set(
                normalize_metadata_tag_slugs(
                    category=course.category,
                    difficulty=course.difficulty,
                )
            )
        )
        try:
            sources.append(
                _candidate_profile(
                    content=content,
                    category=course.category,
                    difficulty=course.difficulty,
                    tag_slugs=tags,
                )
            )
        except ValueError:
            error_count += 1

    targets: list[CandidateContentInput] = []
    for challenge in challenges:
        content = ResolvedKnowledgeContent(
            entity_type="challenge",
            entity_id=str(challenge.id),
            display_name=challenge.name,
            is_active=True,
            href=f"/wargame/{challenge.id}",
            source_updated_at=challenge.updated_at,
        )
        try:
            targets.append(
                _candidate_profile(
                    content=content,
                    category=challenge.category,
                    difficulty=challenge.difficulty,
                    tag_slugs=(
                        challenge_tags.get(challenge.id, set())
                        | set(
                            normalize_metadata_tag_slugs(
                                category=challenge.category,
                                difficulty=challenge.difficulty,
                                tags=challenge.tags if isinstance(challenge.tags, list) else (),
                            )
                        )
                    ),
                )
            )
        except ValueError:
            error_count += 1
    education_sources, education_targets, explicit_candidates, education_errors = (
        await _collect_public_assignment_resource_inputs(db)
    )
    tag_result = await db.execute(
        select(KnowledgeTag)
        .where(KnowledgeTag.id.in_(public_knowledge_tag_ids_query()))
        .order_by(KnowledgeTag.id)
    )
    tag_inputs: list[CandidateContentInput] = []
    for tag in tag_result.scalars().all():
        try:
            tag_content = ResolvedKnowledgeContent(
                entity_type="knowledge_tag",
                entity_id=str(tag.id),
                display_name=tag.name,
                is_active=True,
                href=f"/learn?view=map&focus_type=knowledge_tag&focus_id={tag.id}",
                source_updated_at=tag.updated_at or tag.created_at,
            )
            tag_inputs.append(
                _candidate_profile(
                    content=tag_content,
                    category="knowledge_tag",
                    difficulty=None,
                    tag_slugs=[tag.slug],
                )
            )
        except ValueError:
            error_count += 1
    return CandidateInputCollection(
        sources=sources,
        targets=targets,
        error_count=error_count + education_errors,
        explicit_inputs=[*education_sources, *education_targets],
        explicit_candidates=explicit_candidates,
        tag_inputs=tag_inputs,
    )


async def _collect_public_assignment_resource_inputs(
    db: AsyncSession,
) -> tuple[
    list[CandidateContentInput],
    list[CandidateContentInput],
    list[DeterministicTagPreparesForCandidate],
    int,
]:
    """교육 리소스 링크를 근거로 과제-콘텐츠 후보 입력을 수집한다.

    과제 본문·학생 제출물·채점 피드백은 읽지 않는다. 공개된 강의/과제와
    공개 resolver가 확인한 리소스 식별자만 사용한다.
    """
    result = await db.execute(
        select(EducationAssignment, TeachingClass, EducationResourceLink)
        .join(TeachingClass, TeachingClass.id == EducationAssignment.class_id)
        .join(EducationResourceLink, EducationResourceLink.class_id == TeachingClass.id)
        .where(
            TeachingClass.status == "published",
            EducationAssignment.status == "published",
        )
        .order_by(EducationAssignment.id, EducationResourceLink.id)
    )
    assignment_inputs: dict[str, CandidateContentInput] = {}
    resource_inputs: dict[tuple[str, str], CandidateContentInput] = {}
    candidates: list[DeterministicTagPreparesForCandidate] = []
    error_count = 0

    for assignment, teaching_class, resource in result.all():
        assignment_id = str(assignment.id)
        assignment_input = assignment_inputs.get(assignment_id)
        if assignment_input is None:
            try:
                assignment_content = ResolvedKnowledgeContent(
                    entity_type="assignment",
                    entity_id=assignment_id,
                    display_name=assignment.title,
                    is_active=True,
                    href=f"/learn/classes/{teaching_class.id}?assignment={assignment.id}",
                    source_updated_at=assignment.updated_at,
                )
                assignment_input = _candidate_profile(
                    content=assignment_content,
                    category="education_assignment",
                    difficulty=None,
                    tag_slugs=(),
                )
                assignment_inputs[assignment_id] = assignment_input
            except ValueError:
                error_count += 1
                continue

        target_type = {
            "course": "course",
            "lesson": "lesson",
            "wargame": "challenge",
        }.get(resource.resource_type)
        target_id = resource.course_id or resource.lesson_id or resource.challenge_id
        if not target_type or target_id is None:
            error_count += 1
            continue
        target_key = (target_type, str(target_id))
        target_input = resource_inputs.get(target_key)
        if target_input is None:
            target_content = await resolve_content(
                db,
                target_type,
                str(target_id),
                public_only=True,
            )
            if not target_content:
                continue
            try:
                target_input = _candidate_profile(
                    content=target_content,
                    category=None,
                    difficulty=None,
                    tag_slugs=(),
                )
                resource_inputs[target_key] = target_input
            except ValueError:
                error_count += 1
                continue

        candidate = deterministic_assignment_resource_helps_candidate(
            assignment_input.profile,
            target_input.profile,
        )
        if candidate:
            candidates.append(candidate)

    return list(assignment_inputs.values()), list(resource_inputs.values()), candidates, error_count


def _snapshot_timestamp(value: datetime | None) -> str | None:
    """안전한 노드 메타데이터 시각을 UTC 문자열로 정규화한다.

    레거시 DB의 naive timestamp는 시간대를 추정해 기록하지 않는다. 후보 규칙의 입력은
    allowlist profile digest이므로, timestamp를 억지로 보정하는 것보다 ``None``으로 남겨
    재현성 원장을 오염시키지 않는 편이 안전하다.
    """
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _candidate_node_snapshot(
    item: CandidateContentInput,
    node: KnowledgeContentNode | None,
) -> dict[str, Any]:
    """입력 시점의 원본/registry node 식별 상태만 남긴다.

    ``display_name``은 이미 allowlist profile의 ``title``로 저장되므로 중복 저장하지
    않는다. 본문, URL, 플래그, 제출 데이터는 이 구조에 들어갈 수 없다.
    """
    registry_snapshot: dict[str, Any]
    if node is None:
        registry_snapshot = {
            "state": "absent",
            "node_id": None,
            "is_active": None,
            "source_updated_at": None,
        }
    else:
        registry_snapshot = {
            "state": "present",
            "node_id": str(node.id),
            "is_active": bool(node.is_active),
            "source_updated_at": _snapshot_timestamp(node.source_updated_at),
        }
    return {
        "entity_type": item.content.entity_type,
        "entity_id": item.content.entity_id,
        "source_is_active": bool(item.content.is_active),
        "source_updated_at": _snapshot_timestamp(item.content.source_updated_at),
        "registry": registry_snapshot,
    }


def candidate_input_snapshot(
    inputs: Iterable[CandidateContentInput],
    *,
    nodes_by_content_key: Mapping[tuple[str, str], KnowledgeContentNode] | None = None,
) -> list[dict[str, Any]]:
    """재현 가능한 실행 원장용 allowlist profile/node 스냅샷을 만든다.

    이 함수는 후보 규칙에 이미 전달 가능한 ``profile``과 당시의 안전한 registry node
    식별 상태만 복제한다. 원본 본문·플래그·힌트·첨부파일·제출·사용자 데이터는 수집
    경로와 이 snapshot 모두에 들어갈 수 없다.
    """
    nodes = nodes_by_content_key or {}
    values = [
        {
            "entity_type": item.content.entity_type,
            "entity_id": item.content.entity_id,
            "profile_digest": item.profile_digest,
            "profile": normalize_allowed_profile(item.profile),
            "node": _candidate_node_snapshot(
                item,
                nodes.get((item.content.entity_type, item.content.entity_id)),
            ),
        }
        for item in inputs
    ]
    values.sort(key=lambda value: (value["entity_type"], value["entity_id"]))
    return values


def candidate_input_snapshot_digest(
    inputs: Iterable[CandidateContentInput],
    *,
    nodes_by_content_key: Mapping[tuple[str, str], KnowledgeContentNode] | None = None,
) -> str:
    """allowlist profile/node 입력 snapshot의 무결성 식별자."""
    values = candidate_input_snapshot(inputs, nodes_by_content_key=nodes_by_content_key)
    return digest_json(values)


async def existing_content_nodes(
    db: AsyncSession,
    inputs: Iterable[CandidateContentInput],
) -> dict[tuple[str, str], KnowledgeContentNode]:
    """읽기 전용 중복 점검을 위한 현재 registry 노드를 한 번에 읽는다."""
    keys = sorted({(item.content.entity_type, item.content.entity_id) for item in inputs})
    if not keys:
        return {}
    result = await db.execute(
        select(KnowledgeContentNode).where(
            tuple_(KnowledgeContentNode.entity_type, KnowledgeContentNode.entity_id).in_(keys)
        )
    )
    return {(node.entity_type, node.entity_id): node for node in result.scalars().all()}


async def ensure_safe_content_profile(
    db: AsyncSession,
    *,
    node: KnowledgeContentNode,
    profile: dict[str, Any],
) -> KnowledgeContentProfile:
    """allowlist 프로필을 immutable digest snapshot으로 저장하거나 재사용한다."""
    normalized = normalize_allowed_profile(profile)
    profile_digest = digest_json(normalized)
    statement = (
        pg_insert(KnowledgeContentProfile)
        .values(
            id=uuid4(),
            node_id=node.id,
            content_revision=profile_digest,
            profile_schema_version=SAFE_PROFILE_SCHEMA_VERSION,
            allowed_profile=normalized,
            profile_digest=profile_digest,
            generated_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(constraint="uq_knowledge_content_profile_revision")
        .returning(KnowledgeContentProfile)
    )
    profile_row = (await db.execute(statement)).scalar_one_or_none()
    if profile_row:
        return profile_row
    profile_result = await db.execute(
        select(KnowledgeContentProfile).where(
            KnowledgeContentProfile.node_id == node.id,
            KnowledgeContentProfile.content_revision == profile_digest,
        )
    )
    profile_row = profile_result.scalar_one_or_none()
    if not profile_row:
        raise RuntimeError("지식 콘텐츠 프로필 스냅샷을 확인할 수 없습니다.")
    return profile_row


async def ensure_candidate_node_and_profile(
    db: AsyncSession,
    item: CandidateContentInput,
) -> tuple[KnowledgeContentNode, KnowledgeContentProfile]:
    """실제 후보 저장 시에만 registry node/profile을 upsert한다."""
    node = await ensure_content_node(db, item.content)
    profile = await ensure_safe_content_profile(db, node=node, profile=item.profile)
    return node, profile

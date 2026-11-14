"""Database-independent rules for course prerequisite management.

The functions in this module deliberately operate on already-resolved public
facts.  They are shared by the API write path and the learner response path,
but never open a database connection or write a relation.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol
from uuid import UUID


_APPROVED_STATUS = "approved"


class CoursePublicState(Protocol):
    """The minimum resolved course state needed for prerequisite decisions."""

    is_deleted: bool
    is_published: bool
    status: str


def is_public_course(course: CoursePublicState) -> bool:
    """Return whether a course may be exposed as a mandatory prerequisite."""
    return (
        not course.is_deleted
        and course.is_published
        and course.status == _APPROVED_STATUS
    )


def would_create_course_prerequisite_cycle(
    *,
    course_id: UUID,
    prerequisite_ids: Iterable[UUID],
    existing_edges: Iterable[tuple[UUID, UUID]],
) -> bool:
    """Return whether replacing ``course_id`` prerequisites creates a cycle.

    Edge direction is ``course -> prerequisite``.  The caller omits the
    current course's previous outgoing edges because a replacement discards
    them before the submitted edges are persisted.
    """
    requested_ids = set(prerequisite_ids)
    adjacency: dict[UUID, set[UUID]] = {}
    for source_id, target_id in existing_edges:
        adjacency.setdefault(source_id, set()).add(target_id)
    adjacency[course_id] = requested_ids

    for prerequisite_id in requested_ids:
        seen: set[UUID] = set()
        to_visit = [prerequisite_id]
        while to_visit:
            current_id = to_visit.pop()
            if current_id == course_id:
                return True
            if current_id in seen:
                continue
            seen.add(current_id)
            to_visit.extend(adjacency.get(current_id, set()) - seen)
    return False


def build_course_prerequisite_access(
    *,
    course_id: UUID,
    is_authenticated: bool,
    locked: bool,
    prerequisite_details: Iterable[dict],
) -> dict:
    """Build a public-safe explanation for the course access response.

    ``prerequisite_details`` must already be filtered to publicly available
    courses by the database-backed caller.  This helper never resolves raw
    prerequisite rows, so it cannot leak an archived or unpublished title.
    """
    details = [dict(detail) for detail in prerequisite_details]
    course_href = f"/learn/{course_id}"
    if not locked:
        return {
            "access_state": "available",
            "reason_code": "all_prerequisites_completed",
            "next_action": {"kind": "open_course", "href": course_href},
        }
    if not is_authenticated:
        return {
            "access_state": "sign_in_required",
            "reason_code": "authentication_required",
            "next_action": {"kind": "sign_in", "href": f"/login?next={course_href}"},
        }

    first_incomplete = next(
        (detail for detail in details if not bool(detail.get("completed"))),
        None,
    )
    if first_incomplete is not None:
        prerequisite_id = str(first_incomplete["id"])
        return {
            "access_state": "locked",
            "reason_code": "prerequisites_incomplete",
            "next_action": {
                "kind": "open_prerequisite_course",
                "href": f"/learn/{prerequisite_id}",
            },
        }

    # A malformed legacy state must not fabricate a target or reveal hidden
    # metadata.  The generic support route is the safe recovery action.
    return {
        "access_state": "locked",
        "reason_code": "required_content_unavailable",
        "next_action": {"kind": "contact_admin", "href": "/inquiry"},
    }

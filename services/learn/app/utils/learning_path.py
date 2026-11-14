"""Authoritative, read-only learning-path projection helpers.

The prerequisite tables remain the source of truth for access checks.  This
module intentionally has no database writes and does not create knowledge
relations: it only turns already-resolved, public prerequisite facts into a
stable API projection or a deterministic system-edge identifier.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid5


_COMPLETED_STATUS = "completed"
_UNAVAILABLE_STATUS = "unavailable"
_INCOMPLETE_STATUSES = {"not_started", "in_progress", "unknown"}


def system_prerequisite_edge_id(
    *,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
) -> UUID:
    """Return a deterministic ID for a derived, non-persisted prerequisite edge.

    The namespace makes the value distinct from database relation IDs in
    practice while allowing a client to keep a stable graph key across reads.
    It is deliberately not written to ``knowledge_relations``.
    """
    edge_key = ":".join(
        (
            "ctf-web",
            "system-prerequisite-v1",
            source_type,
            str(source_id),
            target_type,
            str(target_id),
        )
    )
    return uuid5(NAMESPACE_URL, edge_key)


def _is_completed(step: Mapping[str, object]) -> bool:
    return step.get("status") == _COMPLETED_STATUS


def _first_actionable_incomplete_step(
    steps: Iterable[Mapping[str, object]],
) -> Mapping[str, object] | None:
    for step in steps:
        if step.get("status") in _INCOMPLETE_STATUSES:
            return step
    return None


def _next_action(
    *,
    access_state: str,
    reason_code: str,
    target: Mapping[str, object],
    required_steps: list[dict],
) -> dict:
    target_href = str(target["href"])
    if access_state == "available":
        return {"kind": "open_challenge", "href": target_href}
    if access_state == "sign_in_required":
        return {"kind": "sign_in", "href": f"/login?next={target_href}"}
    if reason_code == "required_content_unavailable":
        return {"kind": "contact_admin", "href": "/inquiry"}

    step = _first_actionable_incomplete_step(required_steps)
    if step and step.get("href"):
        return {"kind": "open_content", "href": str(step["href"])}
    # A corrupt legacy row must never grant access.  The generic fallback is
    # intentionally non-sensitive and gives the UI a safe recovery target.
    return {"kind": "contact_admin", "href": "/inquiry"}


def build_challenge_learning_path_projection(
    *,
    target: Mapping[str, object],
    is_authenticated: bool,
    required_steps: Iterable[Mapping[str, object]],
    recommended_steps: Iterable[Mapping[str, object]],
    helpful_content: Iterable[Mapping[str, object]],
) -> dict:
    """Build a response from public, already-resolved source facts.

    ``required_steps`` may only contain public content.  The caller is
    responsible for filtering deleted, unpublished, and competition-only
    records before passing them here; keeping that check at the data boundary
    prevents this pure formatter from becoming an authorization bypass.
    """
    target_data = deepcopy(dict(target))
    required_data = [deepcopy(dict(step)) for step in required_steps]
    recommended_data = [deepcopy(dict(step)) for step in recommended_steps]
    helpful_data = [deepcopy(dict(item)) for item in helpful_content]

    has_unavailable_requirement = any(
        step.get("status") == _UNAVAILABLE_STATUS for step in required_data
    )
    has_incomplete_requirement = any(
        not _is_completed(step) for step in required_data
    )

    if has_unavailable_requirement:
        access_state = "locked"
        reason_code = "required_content_unavailable"
    elif not required_data or not has_incomplete_requirement:
        access_state = "available"
        reason_code = "all_required_steps_completed"
    elif not is_authenticated:
        access_state = "sign_in_required"
        reason_code = "authentication_required"
    else:
        access_state = "locked"
        reason_code = "required_steps_incomplete"

    target_id = str(target_data["id"])
    return {
        "target": target_data,
        "access_state": access_state,
        "reason_code": reason_code,
        "required_steps": required_data,
        "recommended_steps": recommended_data,
        "helpful_content": helpful_data,
        "next_action": _next_action(
            access_state=access_state,
            reason_code=reason_code,
            target=target_data,
            required_steps=required_data,
        ),
        "map_focus_href": (
            "/learn?view=map&focus_type=challenge"
            f"&focus_id={target_id}"
        ),
    }

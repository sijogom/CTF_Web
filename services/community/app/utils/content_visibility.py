"""숨김 콘텐츠의 서버 측 가시성 정책.

숨김은 공개 API에서 존재 여부를 알 수 없도록 처리한다. 관리자·모더레이터는
모더레이션 확인 화면에 필요한 읽기 전용 범위에서만 숨김 대상을 볼 수 있으며,
공개 상호작용 API(댓글, 반응, 구매, 파일 다운로드, 수정, 삭제)에는 이 예외를
사용하지 않는다.
"""

from typing import Literal

from fastapi import HTTPException, status


MODERATION_ROLES = frozenset({"admin", "moderator"})
PUBLIC_ACCESS = "public"
MODERATION_READ_ACCESS = "moderation_read"
VisibilityScope = Literal["public", "moderation_read"]

_NOT_FOUND_DETAIL = "요청한 콘텐츠를 찾을 수 없습니다."


def is_moderation_role(role: str | None) -> bool:
    """숨김 콘텐츠의 제한된 감사 열람이 가능한 역할인지 반환한다."""
    return role in MODERATION_ROLES


def can_access_content(
    *,
    is_hidden: bool,
    role: str | None,
    scope: VisibilityScope = PUBLIC_ACCESS,
) -> bool:
    """가시성 정책을 순수하게 판단한다.

    공개 콘텐츠는 기존 권한·소유권 검사를 그대로 따르며, 숨김 콘텐츠는
    ``moderation_read`` 범위의 관리자·모더레이터만 통과한다. 역할 문자열이
    없거나 알 수 없으면 fail-closed로 처리한다.
    """
    if not is_hidden:
        return True
    return scope == MODERATION_READ_ACCESS and is_moderation_role(role)


def assert_content_visible(
    *,
    is_hidden: bool,
    role: str | None = None,
    scope: VisibilityScope = PUBLIC_ACCESS,
    not_found_detail: str = _NOT_FOUND_DETAIL,
) -> None:
    """숨김 콘텐츠 접근을 일관된 404 응답으로 거부한다.

    404를 사용해 일반 사용자에게 숨김 상태와 리소스 존재 여부를 구분할 수
    있는 단서를 주지 않는다. 호출자는 실제 미존재 대상과 같은
    ``not_found_detail``을 넘겨 응답 본문도 일치시켜야 한다. 공개 API에는
    기본 ``public`` 범위를, 모더레이션 전용 읽기 API에만
    ``moderation_read`` 범위를 명시한다.
    """
    if not can_access_content(is_hidden=is_hidden, role=role, scope=scope):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )

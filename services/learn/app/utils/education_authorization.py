"""교육 운영 도메인 전용 인가 경계.

전역 Auth 서비스의 ``user``/``moderator``/``admin`` 역할은 여기서 변경하지 않는다.
``professor_admin``은 Learn DB의 활성 grant를 통해서만 만들어지며, 교육 라우터에서만
``admin``과 동등하게 취급한다. 다른 서비스와 전역 관리자 API는 이 모듈을 사용하지
않으므로 권한이 확장되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.database import get_db
from app.models.education import EducationRole, EducationRoleGrant
from app.utils.auth_client import get_current_authorization


_GLOBAL_AUTH_ROLES = frozenset({"user", "moderator", "admin"})


@dataclass(frozen=True)
class EducationPrincipal:
    """현재 요청의 Auth 기준 신원과 교육 권한 계산 결과."""

    user_id: UUID
    global_role: str
    has_professor_admin_grant: bool

    @property
    def is_global_admin(self) -> bool:
        return self.global_role == "admin"

    @property
    def is_education_manager(self) -> bool:
        """교육 도메인에서만 global admin과 professor_admin을 동등 처리한다."""
        return self.is_global_admin or self.has_professor_admin_grant


async def resolve_education_principal(
    *,
    user_id: UUID,
    db: AsyncSession,
) -> EducationPrincipal:
    """현재 Auth 상태를 다시 확인한 뒤에만 교육 grant를 읽는다.

    ``get_current_user_id``도 Auth 재검증을 수행하지만, 이 함수는 교육 권한 판정의
    바로 앞에서 한 번 더 Auth 기준 상태(비활성·밴·전역 role)를 확인한다. 네트워크,
    응답 형식, DB 읽기 중 하나라도 실패하면 권한을 허용하지 않는다.
    """
    # 이 호출은 grant SELECT보다 반드시 앞선다. Auth가 장애이거나 대상이 밴/비활성화
    # 되었을 때 이전에 남은 Learn DB 권한 행으로 접근이 열리는 것을 막는다.
    is_active, global_role = await get_current_authorization(str(user_id))
    if not is_active or global_role not in _GLOBAL_AUTH_ROLES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 비활성화된 사용자입니다.",
        )

    if global_role == "admin":
        return EducationPrincipal(
            user_id=user_id,
            global_role=global_role,
            has_professor_admin_grant=False,
        )

    try:
        result = await db.execute(
            select(EducationRoleGrant.id).where(
                EducationRoleGrant.subject_id == user_id,
                EducationRoleGrant.role == EducationRole.PROFESSOR_ADMIN.value,
                EducationRoleGrant.revoked_at.is_(None),
            )
        )
    except SQLAlchemyError as exc:
        # 권한 검증에 필요한 Learn DB가 실패한 경우에도 기존 토큰 역할로 허용하지 않는다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="교육 권한을 확인할 수 없습니다.",
        ) from exc

    return EducationPrincipal(
        user_id=user_id,
        global_role=global_role,
        has_professor_admin_grant=result.scalar_one_or_none() is not None,
    )


async def get_education_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> EducationPrincipal:
    """FastAPI 라우터용 인증된 교육 principal 의존성."""
    # request는 dependency cache와 동일한 요청 경계를 명시하는 용도다. 실제 JWT 파싱은
    # get_current_user_id가 담당하며, 아래에서는 Auth 서비스 기준 상태를 다시 확인한다.
    del request
    return await resolve_education_principal(user_id=user_id, db=db)


def require_education_manager(principal: EducationPrincipal) -> EducationPrincipal:
    if not principal.is_education_manager:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="교육 관리 권한이 없습니다.")
    return principal


def require_global_admin(principal: EducationPrincipal) -> EducationPrincipal:
    """권한 grant/revoke는 Auth의 실제 global admin만 수행할 수 있다."""
    if not principal.is_global_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="최고 관리자 권한이 필요합니다.")
    return principal

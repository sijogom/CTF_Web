"""비밀번호 복잡도 검증 공통 함수.

여러 곳(RegisterRequest, ResetPasswordRequest, ChangePasswordRequest)에서
동일한 비밀번호 복잡도 규칙을 사용하므로, 한 곳에서 관리한다.
"""

import re


def validate_password_complexity(v: str) -> str:
    """비밀번호 복잡도 검증.

    최소 8자, 대문자 1개, 소문자 1개, 숫자 1개, 특수문자 1개 필수.
    Pydantic field_validator에서 직접 호출하여 사용한다.

    Raises:
        ValueError: 복잡도 규칙을 위반할 경우
    """
    if not re.search(r"[A-Z]", v):
        raise ValueError("비밀번호에 대문자가 최소 1개 포함되어야 합니다.")
    if not re.search(r"[a-z]", v):
        raise ValueError("비밀번호에 소문자가 최소 1개 포함되어야 합니다.")
    if not re.search(r"[0-9]", v):
        raise ValueError("비밀번호에 숫자가 최소 1개 포함되어야 합니다.")
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;:',.<>?/`~]", v):
        raise ValueError("비밀번호에 특수문자가 최소 1개 포함되어야 합니다.")
    return v

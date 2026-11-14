"""일반 사용자 문제 등록 요청의 플래그 정책.

동적 인스턴스 플래그는 환경 검증·런타임 발급·폐기 계약과 함께만 만들 수 있다.
일반 사용자의 검토 대기 문제 제출은 그 계약을 표현하지 않으므로 static/regex만
허용한다.
"""

USER_SUBMISSION_FLAG_TYPES = frozenset({"static", "regex"})


def user_submission_flag_type_is_allowed(flag_type: str) -> bool:
    """일반 사용자 제출이 승인될 수 있는 플래그 유형인지 반환한다."""
    return flag_type in USER_SUBMISSION_FLAG_TYPES

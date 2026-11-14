import bcrypt

# bcrypt rounds=12 — 모든 해싱에서 이 모듈의 함수를 사용한다.
# 각 파일에서 별도로 bcrypt 로직을 구현하지 말 것.
_ROUNDS = 12


class _PwdContext:
    """passlib.CryptContext 호환 인터페이스 — bcrypt 직접 사용."""

    def hash(self, password: str) -> str:
        """비밀번호를 bcrypt로 해싱."""
        salt = bcrypt.gensalt(rounds=_ROUNDS)
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    def verify(self, password: str, hashed: str) -> bool:
        """비밀번호와 해시 비교."""
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


pwd_context = _PwdContext()

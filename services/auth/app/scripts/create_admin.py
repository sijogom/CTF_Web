import asyncio
import argparse
import os
from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.security import pwd_context  # 공유 bcrypt 인스턴스 (rounds=12)

# 기본값: 환경 변수 → CLI 인수 순서로 결정
DEFAULTS = {
    "email": os.getenv("ADMIN_EMAIL", "admin@example.com"),
    "username": os.getenv("ADMIN_USERNAME", "admin"),
    "password": os.getenv("ADMIN_PASSWORD", None),
}


_PASSWORD_PLACEHOLDERS = frozenset({
    "changeme", "change-me", "change_me", "replace-me", "replace_me",
    "your-password", "your_password", "your-admin-password", "your_admin_password",
    "placeholder",
})


def is_configured_admin_password(password: str | None) -> bool:
    if not isinstance(password, str) or not password.strip():
        return False
    normalized = password.strip().casefold()
    if normalized in _PASSWORD_PLACEHOLDERS:
        return False
    if normalized.startswith(("changeme_", "change-me-", "change_me_", "replace-me-", "replace_me_")):
        return False
    return not (
        (normalized.startswith("<") and normalized.endswith(">"))
        or (normalized.startswith("${") and normalized.endswith("}"))
    )


async def create_admin(
    email: str,
    username: str,
    password: str | None,
    *,
    update_existing: bool = True,
):
    if not is_configured_admin_password(password):
        raise ValueError("관리자 비밀번호를 ADMIN_PASSWORD 또는 --password로 명시적으로 설정해야 합니다.")
    if not username or not email:
        print("관리자 계정 생성 건너뜀: ADMIN_USERNAME/ADMIN_EMAIL/ADMIN_PASSWORD가 비어 있습니다.")
        return

    async with async_session() as db:
        # role=admin인 부트스트랩 계정 검색 (username 또는 email 매칭)
        result = await db.execute(
            select(User).where(User.role == "admin").where(
                (User.username == username) | (User.email == email)
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            if not update_existing:
                print("기존 관리자 계정을 유지합니다. 부트스트랩으로 계정 정보를 변경하지 않습니다.")
                return
            # .env 변경 사항 동기화 (이메일, 비밀번호 등)
            changed = []
            if existing.email != email:
                existing.email = email
                changed.append("email")
            if existing.username != username:
                existing.username = username
                changed.append("username")
            if not pwd_context.verify(password, existing.password_hash):
                existing.password_hash = pwd_context.hash(password)
                changed.append("password")
            if changed:
                await db.commit()
                print(f"Admin 계정 업데이트: {username} ({email}) — 변경: {', '.join(changed)}")
            else:
                print(f"Admin 계정 확인 완료: {username} ({email})")
            return

        admin = User(
            email=email,
            username=username,
            password_hash=pwd_context.hash(password),
            role="admin",
            is_active=True,
            email_verified=True,
        )
        db.add(admin)
        await db.commit()
        print(f"Admin 계정 생성 완료: {username} ({email})")
        print("보안 경고: 운영 배포 후 반드시 웹 UI에서 비밀번호를 변경하세요.")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="최초 Admin 계정 생성")
    parser.add_argument("--email", default=DEFAULTS["email"])
    parser.add_argument("--username", default=DEFAULTS["username"])
    parser.add_argument("--password", default=DEFAULTS["password"])
    args = parser.parse_args(argv)
    if not is_configured_admin_password(args.password):
        parser.error("관리자 비밀번호를 ADMIN_PASSWORD 또는 --password로 명시적으로 설정해야 합니다.")
    asyncio.run(create_admin(args.email, args.username, args.password))


if __name__ == "__main__":
    main()

"""Discord 사전 연결 로그인 SSO의 입력·PKCE 유틸리티.

이 모듈은 Redis, HTTP, DB에 의존하지 않아 OAuth state 계약을 단위 테스트로
고정할 수 있다. 실제 state의 저장·소비와 Discord 호출은 API 계층의 책임이다.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


DEFAULT_POST_LOGIN_PATH = "/wargame"
MAX_REDIRECT_LENGTH = 512


@dataclass(frozen=True)
class DiscordSSOState:
    """서버가 Redis에 보관하는 일회용 OAuth state의 공개 가능한 부분."""

    state: str
    code_verifier: str
    code_challenge: str


def normalize_post_login_path(value: str | None) -> str:
    """외부 리다이렉트를 막고 플랫폼 내부 상대 경로만 돌려준다."""
    if not value or len(value) > MAX_REDIRECT_LENGTH:
        return DEFAULT_POST_LOGIN_PATH

    decoded = unquote(value)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        return DEFAULT_POST_LOGIN_PATH
    if "\\" in decoded:
        return DEFAULT_POST_LOGIN_PATH

    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return DEFAULT_POST_LOGIN_PATH
    if decoded.startswith("//") or parsed.path.startswith("//"):
        return DEFAULT_POST_LOGIN_PATH
    return decoded


def create_discord_sso_state() -> DiscordSSOState:
    """PKCE S256과 함께 사용할 충분한 엔트로피의 OAuth state를 만든다."""
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return DiscordSSOState(
        state=state,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
    )


def discord_oauth_configured(client_id: str, client_secret: str) -> bool:
    """Discord OAuth 클라이언트 설정의 최소 충족 여부를 확인한다."""
    return bool(client_id and client_secret)

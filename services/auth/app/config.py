from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_AUTH_DIR = Path(__file__).resolve().parent.parent
_LEGACY_PRIVATE_KEY_PATH = _AUTH_DIR / "jwt_private.pem"
_LEGACY_PUBLIC_KEY_PATH = _AUTH_DIR / "jwt_public.pem"


def _read_pem_file(file_path: str, *, setting_name: str) -> str:
    """명시한 런타임 key 파일을 읽고 누락·빈 파일을 즉시 거부한다."""
    path = Path(file_path)
    try:
        if not path.is_file():
            raise ValueError(f"{setting_name} 경로에 PEM 파일이 없습니다.")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"{setting_name} 파일을 읽을 수 없습니다.") from error
    if not value:
        raise ValueError(f"{setting_name} 파일이 비어 있습니다.")
    return value


def _read_legacy_pem(path: Path) -> str:
    """Docker 외 개발 직접 실행 호환용 폴백. production에서는 사용하지 않는다."""
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://auth_svc:changeme_auth@postgres:5432/platform_db"

    # Production RS256 key는 Docker secret 파일에서만 읽는다.
    jwt_secret_key: str = ""
    jwt_public_key: str = ""
    jwt_secret_key_file: str = ""
    jwt_public_key_file: str = ""
    jwt_algorithm: str = "RS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_member_role_id: str = ""

    learn_service_url: str = "http://localhost:8100"
    community_service_url: str = "http://localhost:8300"
    env_orchestrator_url: str = "http://localhost:8200"
    ctfd_url: str = "http://ctfd:8010"
    ctfd_admin_token: str = ""
    redis_url: str = "redis://:@localhost:6379/0"

    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from: str = "noreply@ctf.example.ac.kr"
    smtp_tls: bool = False
    smtp_user: str = ""
    smtp_password: str = ""
    frontend_url: str = "http://localhost"

    email_verify_expire_hours: int = 24
    email_verify_code_expire_minutes: int = 10
    email_verify_code_max_attempts: int = 5
    email_verify_resend_cooldown_minutes: int = 2
    password_reset_expire_hours: int = 1
    oauth_state_expire_minutes: int = 10
    email_unsub_expire_days: int = 7

    admin_username: str = ""
    admin_email: str = ""
    admin_password: str = ""
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_secure: bool = False
    internal_api_key: str = ""
    environment: str = "development"
    site_domain: str = ""
    cors_allow_methods: list[str] = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    cors_allow_headers: list[str] = ["Content-Type", "Authorization", "X-CSRF-Token"]
    frontend_port: int = 3100

    @property
    def cors_origins(self) -> list[str]:
        origins = []
        if not self.is_production:
            origins.extend(["http://localhost", f"http://localhost:{self.frontend_port}"])
        if self.site_domain:
            origins.extend([f"https://{self.site_domain}", f"http://{self.site_domain}"])
        return origins

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _resolve_jwt_keys(self) -> "Settings":
        inline_secret = self.jwt_secret_key
        inline_public = self.jwt_public_key

        if self.is_production and self.jwt_algorithm == "RS256":
            if inline_secret or inline_public:
                raise ValueError("production RS256에서는 JWT PEM 본문 환경변수를 허용하지 않습니다.")
            if not self.jwt_secret_key_file or not self.jwt_public_key_file:
                raise ValueError("production RS256에는 JWT_SECRET_KEY_FILE과 JWT_PUBLIC_KEY_FILE이 필요합니다.")

        if self.jwt_secret_key_file:
            self.jwt_secret_key = _read_pem_file(
                self.jwt_secret_key_file,
                setting_name="JWT_SECRET_KEY_FILE",
            )
        elif not self.jwt_secret_key and not self.is_production:
            self.jwt_secret_key = _read_legacy_pem(_LEGACY_PRIVATE_KEY_PATH)

        if self.jwt_public_key_file:
            self.jwt_public_key = _read_pem_file(
                self.jwt_public_key_file,
                setting_name="JWT_PUBLIC_KEY_FILE",
            )
        elif not self.jwt_public_key and not self.is_production:
            self.jwt_public_key = _read_legacy_pem(_LEGACY_PUBLIC_KEY_PATH)
        return self


settings = Settings()

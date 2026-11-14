from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SERVICE_DIR = Path(__file__).resolve().parent.parent
_AUTH_DIR = _SERVICE_DIR.parent / "auth"
_LEGACY_PUBLIC_KEY_PATHS = [_SERVICE_DIR / "jwt_public.pem", _AUTH_DIR / "jwt_public.pem"]


def _read_pem_file(file_path: str, *, setting_name: str) -> str:
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


def _find_legacy_public_key() -> str:
    for path in _LEGACY_PUBLIC_KEY_PATHS:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return ""


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://community_svc:CHANGE_ME@postgres:5432/community_db"
    auth_service_url: str = "http://localhost:8020"
    jwt_public_key: str = ""
    jwt_public_key_file: str = ""
    jwt_algorithm: str = "RS256"
    internal_api_key: str = ""
    environment: str = "development"
    frontend_port: int = 3100
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_secure: bool = False
    site_domain: str = ""
    cors_allow_methods: list[str] = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    cors_allow_headers: list[str] = ["Content-Type", "Authorization", "X-CSRF-Token"]

    @property
    def cors_origins(self) -> list[str]:
        origins = []
        if not self.is_production:
            origins.extend(["http://localhost", f"http://localhost:{self.frontend_port}"])
        if self.site_domain:
            origins.append(f"https://{self.site_domain}")
        return origins

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    model_config = SettingsConfigDict(env_file=".env")

    @model_validator(mode="after")
    def _resolve_jwt_public_key(self) -> "Settings":
        inline_public = self.jwt_public_key
        if self.is_production and self.jwt_algorithm == "RS256":
            if inline_public:
                raise ValueError("production RS256에서는 JWT_PUBLIC_KEY 본문 환경변수를 허용하지 않습니다.")
            if not self.jwt_public_key_file:
                raise ValueError("production RS256에는 JWT_PUBLIC_KEY_FILE이 필요합니다.")

        if self.jwt_public_key_file:
            self.jwt_public_key = _read_pem_file(
                self.jwt_public_key_file,
                setting_name="JWT_PUBLIC_KEY_FILE",
            )
        elif not self.jwt_public_key and not self.is_production:
            self.jwt_public_key = _find_legacy_public_key()
        return self


settings = Settings()

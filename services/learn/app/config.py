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
    database_url: str = "postgresql+asyncpg://learn_svc:CHANGE_ME@postgres:5432/learn_db"
    auth_service_url: str = "http://localhost:8020"
    env_orchestrator_url: str = "http://localhost:8200"
    jwt_public_key: str = ""
    jwt_public_key_file: str = ""
    jwt_algorithm: str = "RS256"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_secure: bool = False
    redis_url: str = "redis://:@localhost:6379/0"
    internal_api_key: str = ""
    # 문제 등록 요청의 static/regex 플래그는 새 행부터 Fernet 암호문으로만 보관한다.
    # 키가 없으면 원문 fallback을 만들지 않고 해당 요청을 fail-closed로 거절한다.
    flag_storage_encryption_key: str = ""
    flag_storage_encryption_key_version: str = "fernet-v1"
    submit_rate_limit: int = 10
    # 동적 인스턴스 플래그는 기본 비활성이다. 활성화해도 keyring이 없으면 발급·검증
    # 경로가 fail-closed로 거절하며, 플랫폼 전체가 시작 불가 상태가 되지는 않는다.
    dynamic_instance_flags_enabled: bool = False
    # `version=base64url-32-byte-key[,next=...]` 형식. 현재 key와 만료 전 이전 key를
    # 함께 둘 수 있어 issuance의 hmac_key_version 기반 key rotation을 지원한다.
    # 값은 절대로 로그·응답·문서에 출력하지 않는다.
    dynamic_flag_hmac_keyring: str = ""
    # A real scanner adapter is a production prerequisite for education file
    # uploads.  The development bypass is explicit and cannot bypass the
    # production check in the scanner boundary.
    education_asset_scan_development_bypass: bool = False
    # The scanner is reachable only through the private Compose scanner network.
    # These timeout caps bound the duration a request can wait for a trustworthy
    # clamd verdict; any failure is handled as a storage-denying scanner error.
    education_asset_scanner_host: str = "clamav"
    education_asset_scanner_port: int = 3310
    education_asset_scanner_connect_timeout_seconds: float = 3.0
    education_asset_scanner_io_timeout_seconds: float = 150.0
    environment: str = "development"
    frontend_port: int = 3100
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
        if not self.education_asset_scanner_host or any(
            character.isspace() for character in self.education_asset_scanner_host
        ):
            raise ValueError("EDUCATION_ASSET_SCANNER_HOST must be a non-empty host name")
        if not 1 <= self.education_asset_scanner_port <= 65535:
            raise ValueError("EDUCATION_ASSET_SCANNER_PORT must be between 1 and 65535")
        if not 0 < self.education_asset_scanner_connect_timeout_seconds <= 30:
            raise ValueError("EDUCATION_ASSET_SCANNER_CONNECT_TIMEOUT_SECONDS must be between 0 and 30")
        if not 0 < self.education_asset_scanner_io_timeout_seconds <= 300:
            raise ValueError("EDUCATION_ASSET_SCANNER_IO_TIMEOUT_SECONDS must be between 0 and 300")

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

"""관리자 운영 설정의 영속 저장·캐시 경계를 한곳에서 관리한다.

PostgreSQL ``admin_settings``가 기준 데이터이고 Redis는 빠른 전달용 캐시다.
설정 저장 성공을 Redis 성공만으로 판단하지 않으며, SMTP 발송기와 가입 라우트도
같은 읽기 경로를 사용해야 한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.redis import redis_client
from app.utils.crypto import encrypt_value

logger = logging.getLogger(__name__)

SETTINGS_CACHE_KEY = "admin:settings"
SETTINGS_DB_KEY = "global"
SMTP_SETTINGS_KEYS = frozenset(
    {
        "smtp_host",
        "smtp_port",
        "smtp_from",
        "smtp_tls",
        "smtp_user",
        "smtp_password",
    }
)
ADMIN_SETTINGS_UNAVAILABLE_DETAIL = "관리자 설정을 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
SMTP_SETTINGS_INVALID_DETAIL = "SMTP 설정을 확인해 주세요."


class AdminSettingsStorageError(RuntimeError):
    """관리자 설정 기준 저장소를 읽거나 쓸 수 없을 때 발생한다."""


class InvalidSmtpSettings(ValueError):
    """SMTP 설정의 형식이나 필수 연결 값이 유효하지 않을 때 발생한다."""


def prepare_smtp_settings_update(
    values: Mapping[str, Any],
    *,
    require_connection_fields: bool = False,
) -> dict[str, Any]:
    """SMTP 입력을 한 경로에서 검증·정규화·암호화한다.

    전용 SMTP API는 연결 필수값을 모두 요구하고, 기존 일반 설정 API는 부분 PATCH를
    호환 처리한다. 빈 비밀번호와 ``__UNCHANGED__`` 표시는 기존 암호문 유지 의미다.
    명시적인 비밀번호 삭제는 운영 정책이 확정되기 전까지 허용하지 않는다.
    """
    updates: dict[str, Any] = {}
    required = ("smtp_host", "smtp_port", "smtp_from")
    if require_connection_fields:
        missing = [key for key in required if key not in values]
        if missing:
            raise InvalidSmtpSettings("SMTP 연결 정보를 모두 입력해 주세요.")

    if "smtp_host" in values:
        host = values["smtp_host"]
        if not isinstance(host, str) or not host.strip():
            raise InvalidSmtpSettings("SMTP 호스트를 확인해 주세요.")
        updates["smtp_host"] = host.strip()

    if "smtp_port" in values:
        port = values["smtp_port"]
        if isinstance(port, bool):
            raise InvalidSmtpSettings("SMTP 포트를 확인해 주세요.")
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as exc:
            raise InvalidSmtpSettings("SMTP 포트를 확인해 주세요.") from exc
        if not 1 <= normalized_port <= 65535:
            raise InvalidSmtpSettings("SMTP 포트를 확인해 주세요.")
        updates["smtp_port"] = normalized_port

    if "smtp_from" in values:
        from_addr = values["smtp_from"]
        if not isinstance(from_addr, str) or not from_addr.strip():
            raise InvalidSmtpSettings("SMTP 발신자 주소를 확인해 주세요.")
        updates["smtp_from"] = from_addr.strip()

    if "smtp_tls" in values:
        tls = values["smtp_tls"]
        if not isinstance(tls, bool):
            raise InvalidSmtpSettings("SMTP TLS 설정을 확인해 주세요.")
        updates["smtp_tls"] = tls

    if "smtp_user" in values:
        user = values["smtp_user"]
        if not isinstance(user, str):
            raise InvalidSmtpSettings("SMTP 사용자명을 확인해 주세요.")
        updates["smtp_user"] = user.strip()

    if "smtp_password" in values:
        password = values["smtp_password"]
        if not isinstance(password, str):
            raise InvalidSmtpSettings("SMTP 비밀번호를 확인해 주세요.")
        if password and password != "__UNCHANGED__":
            updates["smtp_password"] = encrypt_value(password)

    return updates


def default_admin_settings() -> dict[str, Any]:
    """환경 변수 기반 기본값을 반환한다.

    기본값은 초기 설치 호환용일 뿐, 관리자 변경 후에는 DB 값이 항상 우선한다.
    """
    return {
        "site_name": "CUHA Memory",
        "registration_open": True,
        "max_containers": 50,
        "container_timeout": 30,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_from": settings.smtp_from,
        "smtp_tls": settings.smtp_tls,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password,
        "tiers": [],
    }


def _decode_settings(raw: str | bytes | None, source: str) -> dict[str, Any] | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        logger.warning("관리자 설정 %s JSON이 손상되었습니다.", source)
        return None
    if not isinstance(value, dict):
        logger.warning("관리자 설정 %s 값이 객체가 아닙니다.", source)
        return None
    return value


def merge_admin_settings(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """알려진 기본값을 보충하되, 향후 설정 키는 삭제하지 않고 보존한다."""
    merged = default_admin_settings()
    if values:
        merged.update(dict(values))
    return merged


async def _load_settings_from_database(db: AsyncSession) -> dict[str, Any] | None:
    try:
        result = await db.execute(
            text("SELECT value FROM admin_settings WHERE key = :key"),
            {"key": SETTINGS_DB_KEY},
        )
    except Exception as exc:
        raise AdminSettingsStorageError("관리자 설정 기준 저장소를 읽을 수 없습니다.") from exc
    return _decode_settings(result.scalar_one_or_none(), "PostgreSQL")


async def _load_legacy_settings_from_cache() -> dict[str, Any] | None:
    """DB 백업 도입 전 Redis에만 저장된 설정을 무손실로 읽는다."""
    try:
        raw = await redis_client.get(SETTINGS_CACHE_KEY)
    except Exception as exc:
        logger.warning("레거시 관리자 설정 Redis 조회 실패: %s", type(exc).__name__)
        return None
    return _decode_settings(raw, "Redis")


async def load_admin_settings(
    db: AsyncSession,
    *,
    refresh_cache: bool = True,
) -> tuple[dict[str, Any], str]:
    """PostgreSQL을 기준으로 설정을 읽고 Redis 캐시를 최선으로 갱신한다.

    캐시 값이 존재하더라도 DB가 기준이므로, 이전 저장 실패·Redis 재시작·별도 SMTP
    화면에서 발생한 드리프트가 런타임 동작을 바꾸지 않는다.
    """
    stored = await _load_settings_from_database(db)
    source = "database"
    if stored is None:
        # 과거 SMTP 전용 화면은 Redis에만 설정을 저장했다. DB 기준 전환이 기존
        # 발신 설정을 초기화하지 않도록 DB 행이 없을 때만 이 값을 호환 입력으로 쓴다.
        stored = await _load_legacy_settings_from_cache()
        source = "redis_legacy" if stored is not None else "default"
    effective = merge_admin_settings(stored)

    if refresh_cache:
        try:
            await redis_client.set(SETTINGS_CACHE_KEY, json.dumps(effective))
        except Exception as exc:
            logger.warning("관리자 설정 Redis 캐시 갱신 실패: %s", type(exc).__name__)
            return effective, f"{source}_cache_unavailable"
    return effective, source


async def save_admin_settings(db: AsyncSession, values: Mapping[str, Any]) -> None:
    """DB 트랜잭션 안에서 관리자 설정을 저장한다. 호출자가 commit을 책임진다."""
    payload = json.dumps(dict(values), ensure_ascii=False, separators=(",", ":"))
    try:
        await db.execute(
            text(
                "INSERT INTO admin_settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"key": SETTINGS_DB_KEY, "value": payload},
        )
    except Exception as exc:
        raise AdminSettingsStorageError("관리자 설정을 저장할 수 없습니다.") from exc


async def refresh_admin_settings_cache(values: Mapping[str, Any]) -> bool:
    """커밋된 설정만 Redis에 반영한다. 실패는 호출자에게 명시한다."""
    try:
        await redis_client.set(
            SETTINGS_CACHE_KEY,
            json.dumps(dict(values), ensure_ascii=False, separators=(",", ":")),
        )
        return True
    except Exception as exc:
        logger.warning("관리자 설정 Redis 캐시 저장 실패: %s", type(exc).__name__)
        return False


async def load_runtime_admin_settings() -> dict[str, Any]:
    """DB 세션이 없는 SMTP 발송기 등에서 기준 설정을 읽는 진입점이다."""
    async with async_session() as db:
        values, _ = await load_admin_settings(db)
        return values


async def rehydrate_admin_settings_cache() -> bool:
    """시작 시 DB 기준 설정을 Redis에 복구하고 기존 Redis-only 값을 DB로 이전한다."""
    async with async_session() as db:
        stored = await _load_settings_from_database(db)
        if stored is None:
            legacy = await _load_legacy_settings_from_cache()
            if legacy is not None:
                values = merge_admin_settings(legacy)
                try:
                    await save_admin_settings(db, values)
                    await db.commit()
                    logger.info("기존 Redis 관리자 설정을 PostgreSQL 기준 저장소로 이전했습니다.")
                except AdminSettingsStorageError:
                    await db.rollback()
                    logger.exception("기존 Redis 관리자 설정의 PostgreSQL 이전 실패")
                    return False
            else:
                values = default_admin_settings()
        else:
            values = merge_admin_settings(stored)
    return await refresh_admin_settings_cache(values)

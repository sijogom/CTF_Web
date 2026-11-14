import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import partial

from app.utils.admin_settings import load_runtime_admin_settings
from app.utils.crypto import decrypt_value

logger = logging.getLogger(__name__)


def mask_email(email: str) -> str:
    """이메일 주소 마스킹 (로그용). 예: j***@example.com"""
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}" if local else f"***@{domain}"


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    from_addr: str
    tls: bool
    user: str
    password: str


class SmtpConfigurationError(RuntimeError):
    """영속된 SMTP 설정이 발송에 안전하지 않을 때 발생한다."""


def _required_smtp_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SmtpConfigurationError("smtp_configuration_invalid")
    return value.strip()


async def _get_smtp_config() -> SmtpConfig:
    """PostgreSQL 기준 관리자 설정에서 SMTP 구성을 읽는다.

    Redis는 설정 전파용 캐시일 뿐이고, 기준 설정은 DB에서 읽는다. 저장된 값이
    손상됐을 때 환경 변수로 조용히 전환하면 관리자가 저장한 SMTP 설정과 다른
    계정으로 메일이 나갈 수 있으므로 호출자에게 안전한 실패를 전달한다.
    """
    data = await load_runtime_admin_settings()
    raw_port = data.get("smtp_port")
    if isinstance(raw_port, bool):
        raise SmtpConfigurationError("smtp_configuration_invalid")
    try:
        smtp_port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise SmtpConfigurationError("smtp_configuration_invalid") from exc
    if not 1 <= smtp_port <= 65535:
        raise SmtpConfigurationError("smtp_configuration_invalid")

    raw_tls = data.get("smtp_tls")
    if not isinstance(raw_tls, bool):
        raise SmtpConfigurationError("smtp_configuration_invalid")
    raw_password = data.get("smtp_password")
    if not isinstance(raw_password, str):
        raise SmtpConfigurationError("smtp_configuration_invalid")

    return SmtpConfig(
        host=_required_smtp_text(data, "smtp_host"),
        port=smtp_port,
        from_addr=_required_smtp_text(data, "smtp_from"),
        tls=raw_tls,
        user=_required_smtp_text(data, "smtp_user") if data.get("smtp_user") else "",
        password=decrypt_value(raw_password),
    )


def _send_sync(to: str, msg_str: str, cfg: SmtpConfig) -> None:
    """동기 SMTP 전송 (스레드 풀에서 실행)"""
    # port 465 = implicit TLS (SMTP_SSL), 그 외 = STARTTLS 또는 평문
    if cfg.tls and cfg.port == 465:
        with smtplib.SMTP_SSL(cfg.host, cfg.port) as server:
            if cfg.user:
                server.login(cfg.user, cfg.password)
            server.sendmail(cfg.from_addr, to, msg_str)
    else:
        with smtplib.SMTP(cfg.host, cfg.port) as server:
            if cfg.tls:
                server.starttls()
            if cfg.user:
                server.login(cfg.user, cfg.password)
            server.sendmail(cfg.from_addr, to, msg_str)


_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 2, 4]  # 초 단위 지수 백오프


async def send_email(to: str, subject: str, html_body: str) -> None:
    """비동기 이메일 발송 — 실패 시 최대 3회 재시도 (지수 백오프)."""
    cfg = await _get_smtp_config()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))
    msg_str = msg.as_string()

    loop = asyncio.get_running_loop()
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            await loop.run_in_executor(None, partial(_send_sync, to, msg_str, cfg))
            return  # 성공
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_DELAYS[attempt])

    # 모든 재시도 실패 — last_exc는 반드시 할당됨 (_MAX_RETRIES >= 1)
    assert last_exc is not None
    raise last_exc

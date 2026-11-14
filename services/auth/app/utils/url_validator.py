"""SSRF 방지 URL 검증 유틸리티."""

import ipaddress
import socket
from urllib.parse import urlparse

# 내부 IP 대역 (SSRF 차단 대상)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local (AWS 메타데이터 등)
    ipaddress.ip_network("::1/128"),          # IPv6 루프백
    ipaddress.ip_network("fc00::/7"),         # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),        # IPv6 Link-local
]

ALLOWED_SCHEMES = ("http", "https")


def is_safe_url(url: str, allowed_domains: list[str] | None = None) -> bool:
    """
    외부 URL이 안전한지 검증 (SSRF 방지).

    Args:
        url: 검증할 URL
        allowed_domains: 허용할 도메인 목록 (None이면 도메인 제한 없음)

    Returns:
        안전하면 True, 위험하면 False
    """
    try:
        parsed = urlparse(url)

        # 스키마 검증
        if parsed.scheme not in ALLOWED_SCHEMES:
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # 도메인 화이트리스트 검증
        if allowed_domains and hostname not in allowed_domains:
            return False

        # DNS 해석 → IP 주소가 내부 대역인지 확인
        resolved_ips = socket.getaddrinfo(hostname, None)
        for _, _, _, _, addr in resolved_ips:
            ip = ipaddress.ip_address(addr[0])
            for network in BLOCKED_NETWORKS:
                if ip in network:
                    return False

        return True
    except (ValueError, socket.gaierror, OSError):
        return False

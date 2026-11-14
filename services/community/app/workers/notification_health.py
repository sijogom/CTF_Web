"""알림 worker 내부 loopback 상태. 데이터·키·수신자 정보는 반환하지 않는다."""

import asyncio
import json
import time


class NotificationWorkerHealth:
    def __init__(self, port: int, poll_seconds: float, *, baseline_ready=True):
        if not 1 <= port <= 65535:
            raise ValueError("notification health port is outside the allowed range")
        self.port = port
        self.max_age = max(30.0, poll_seconds * 3)
        self.baseline_ready = baseline_ready
        self.last_success = None
        self.server = None

    def mark_success(self):
        self.last_success = time.monotonic()

    def is_ready(self):
        return self.baseline_ready and self.last_success is not None and time.monotonic() - self.last_success <= self.max_age

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, limit=4096)

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer):
        status, body = 400, {"status": "bad_request"}
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=1)
            parts = line.decode("ascii").strip().split()
            if len(parts) == 3 and parts[0] == "GET":
                if parts[1] == "/health":
                    status, body = 200, {"status": "ok"}
                elif parts[1] == "/ready":
                    ready = self.is_ready()
                    status, body = (200 if ready else 503), {"status": "ready" if ready else "not_ready",
                        "baseline_complete": self.baseline_ready}
                else:
                    status, body = 404, {"status": "not_found"}
        except (ValueError, UnicodeError, TimeoutError, OSError):
            pass
        payload = json.dumps(body).encode("ascii")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}[status]
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".encode("ascii") + payload)
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

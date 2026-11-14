"""Unit contracts for the private education-asset clamd scanner adapter.

These tests do not contact Docker, clamd, MinIO, or a real network socket. They
verify the byte framing and fail-closed handling against a small in-memory
socket double; runtime EICAR and container-health verification remains a
separate Linux Docker task.
"""
from __future__ import annotations

import io
import socket
import struct
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.clamd_scanner import (  # noqa: E402
    CLAMD_STREAM_CHUNK_SIZE,
    ClamdMalwareDetectedError,
    ClamdScannerUnavailableError,
    scan_stream_with_clamd,
)


class _FakeClamdSocket:
    def __init__(self, *reply_chunks: bytes) -> None:
        self.reply_chunks = list(reply_chunks)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []

    def __enter__(self) -> "_FakeClamdSocket":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _buffer_size: int) -> bytes:
        return self.reply_chunks.pop(0) if self.reply_chunks else b""


class EducationAssetScannerTests(unittest.TestCase):
    def _scan_with_reply(self, data: bytes, *reply_chunks: bytes) -> tuple[str, _FakeClamdSocket, io.BytesIO]:
        scanner_socket = _FakeClamdSocket(*reply_chunks)
        stream = io.BytesIO(data)
        with patch("app.utils.clamd_scanner.socket.create_connection", return_value=scanner_socket):
            result = scan_stream_with_clamd(
                data_stream=stream,
                byte_size=len(data),
                max_byte_size=250 * 1024 * 1024,
                host="clamav",
                port=3310,
                connect_timeout_seconds=3,
                io_timeout_seconds=150,
            )
        return result, scanner_socket, stream

    def test_clean_reply_uses_nul_framed_instream_and_rewinds_existing_spool(self) -> None:
        data = b"A" * (CLAMD_STREAM_CHUNK_SIZE + 3)
        result, scanner_socket, stream = self._scan_with_reply(data, b"stream: OK\x00")

        expected = b"".join(
            (
                b"zINSTREAM\x00",
                struct.pack("!I", CLAMD_STREAM_CHUNK_SIZE),
                b"A" * CLAMD_STREAM_CHUNK_SIZE,
                struct.pack("!I", 3),
                b"A" * 3,
                struct.pack("!I", 0),
            )
        )
        self.assertEqual(result, "clean")
        self.assertEqual(b"".join(scanner_socket.sent), expected)
        self.assertEqual(stream.tell(), 0)
        self.assertEqual(len(scanner_socket.timeouts), 1)

    def test_found_reply_is_not_stored_as_clean_and_rewinds_stream(self) -> None:
        data = b"safe-looking content"
        scanner_socket = _FakeClamdSocket(b"stream: Eicar-Test-Signature FOUND\x00")
        stream = io.BytesIO(data)

        with (
            patch("app.utils.clamd_scanner.socket.create_connection", return_value=scanner_socket),
            self.assertRaises(ClamdMalwareDetectedError),
        ):
            scan_stream_with_clamd(
                data_stream=stream,
                byte_size=len(data),
                max_byte_size=250 * 1024 * 1024,
                host="clamav",
                port=3310,
                connect_timeout_seconds=3,
                io_timeout_seconds=150,
            )

        self.assertEqual(stream.tell(), 0)

    def test_empty_malformed_or_timeout_reply_fails_closed(self) -> None:
        data = b"safe-looking content"
        for reply in (b"", b"stream: maybe\x00", b"X" * (8 * 1024 + 1)):
            with self.subTest(reply=reply):
                scanner_socket = _FakeClamdSocket(reply)
                stream = io.BytesIO(data)
                with (
                    patch("app.utils.clamd_scanner.socket.create_connection", return_value=scanner_socket),
                    self.assertRaises(ClamdScannerUnavailableError),
                ):
                    scan_stream_with_clamd(
                        data_stream=stream,
                        byte_size=len(data),
                        max_byte_size=250 * 1024 * 1024,
                        host="clamav",
                        port=3310,
                        connect_timeout_seconds=3,
                        io_timeout_seconds=150,
                    )
                self.assertEqual(stream.tell(), 0)

        stream = io.BytesIO(data)
        with (
            patch(
                "app.utils.clamd_scanner.socket.create_connection",
                side_effect=socket.timeout("scanner timeout"),
            ),
            patch("app.utils.clamd_scanner.logger"),
            self.assertRaises(ClamdScannerUnavailableError),
        ):
            scan_stream_with_clamd(
                data_stream=stream,
                byte_size=len(data),
                max_byte_size=250 * 1024 * 1024,
                host="clamav",
                port=3310,
                connect_timeout_seconds=3,
                io_timeout_seconds=150,
            )
        self.assertEqual(stream.tell(), 0)


if __name__ == "__main__":
    unittest.main()

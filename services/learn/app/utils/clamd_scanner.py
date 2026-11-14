"""Bounded ClamAV clamd INSTREAM client for private education uploads.

This module deliberately depends only on the Python standard library.  It can
therefore be unit-tested without a running ClamAV container, MinIO, or FastAPI;
the caller supplies the trusted deployment configuration and maps its errors to
the API's stable response types.
"""
from __future__ import annotations

import logging
import socket
import struct


logger = logging.getLogger(__name__)

CLAMD_STREAM_CHUNK_SIZE = 64 * 1024
CLAMD_MAX_REPLY_BYTES = 8 * 1024


class ClamdScannerUnavailableError(RuntimeError):
    """Clamd did not produce a trustworthy verdict within the configured bounds."""


class ClamdMalwareDetectedError(RuntimeError):
    """Clamd detected malware or a configured scanning-limit violation."""


def _rewind_stream(data_stream) -> None:
    """Rewind the existing multipart spool without materialising another copy."""
    try:
        data_stream.seek(0)
    except (AttributeError, OSError) as exc:
        raise ClamdScannerUnavailableError("upload stream is not seekable") from exc


def _read_nul_framed_reply(scanner_socket: socket.socket) -> str:
    """Read exactly one bounded reply for a ``z``-framed clamd command."""
    reply = bytearray()
    terminated = False

    while len(reply) <= CLAMD_MAX_REPLY_BYTES:
        chunk = scanner_socket.recv(1024)
        if not chunk:
            break
        terminator_index = chunk.find(b"\x00")
        if terminator_index >= 0:
            reply.extend(chunk[:terminator_index])
            terminated = True
            break
        reply.extend(chunk)

    if not terminated or not reply or len(reply) > CLAMD_MAX_REPLY_BYTES:
        raise ClamdScannerUnavailableError("clamd returned no bounded reply")
    return bytes(reply).decode("utf-8", errors="replace").strip()


def _classify_reply(reply: str) -> str:
    """Only an exact ``OK`` verdict permits storage; every other reply denies it."""
    _target, separator, verdict = reply.rpartition(":")
    if not separator:
        raise ClamdScannerUnavailableError("clamd reply has no verdict separator")

    normalized_verdict = verdict.strip()
    if normalized_verdict == "OK":
        return "clean"
    if normalized_verdict.endswith(" FOUND"):
        # Never carry the signature name into an API response or database audit.
        raise ClamdMalwareDetectedError("clamd returned a detection")
    raise ClamdScannerUnavailableError("clamd returned a non-clean verdict")


def scan_stream_with_clamd(
    *,
    data_stream,
    byte_size: int,
    max_byte_size: int,
    host: str,
    port: int,
    connect_timeout_seconds: float,
    io_timeout_seconds: float,
) -> str:
    """Scan a seekable stream through clamd's NUL-framed ``INSTREAM`` protocol.

    The caller is responsible for ensuring that ``host`` is a private service
    endpoint. The transport is intentionally not treated as authenticated or
    encrypted, so it must never be published beyond the scanner network.
    """
    if byte_size <= 0 or byte_size > max_byte_size:
        raise ClamdScannerUnavailableError("upload size is outside scanner contract")

    _rewind_stream(data_stream)
    scanned_size = 0
    try:
        with socket.create_connection((host, port), timeout=connect_timeout_seconds) as scanner_socket:
            scanner_socket.settimeout(io_timeout_seconds)
            scanner_socket.sendall(b"zINSTREAM\x00")

            while chunk := data_stream.read(CLAMD_STREAM_CHUNK_SIZE):
                scanned_size += len(chunk)
                if scanned_size > byte_size:
                    raise ClamdScannerUnavailableError("upload stream grew during scan")
                scanner_socket.sendall(struct.pack("!I", len(chunk)))
                scanner_socket.sendall(chunk)

            if scanned_size != byte_size:
                raise ClamdScannerUnavailableError("upload stream size changed during scan")

            scanner_socket.sendall(struct.pack("!I", 0))
            reply = _read_nul_framed_reply(scanner_socket)
    except (ClamdScannerUnavailableError, ClamdMalwareDetectedError):
        raise
    except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
        # Keep security telemetry useful without writing file contents, a remote
        # endpoint, or a scanner reply into normal application logs.
        logger.warning("Clamd scan request failed: %s", type(exc).__name__)
        raise ClamdScannerUnavailableError("clamd request failed") from exc
    finally:
        # The next MinIO write consumes this same spool only after ``clean``.
        _rewind_stream(data_stream)

    return _classify_reply(reply)

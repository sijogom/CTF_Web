"""MinIO 스트리밍 응답이 early disconnect에도 연결을 반환하는지 고정한다."""

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


class AttachmentStreamCleanupContractTests(unittest.TestCase):
    def test_common_iterator_closes_and_releases_the_minio_response(self) -> None:
        source = (SERVICE_ROOT / "app" / "utils" / "minio_client.py").read_text(encoding="utf-8")
        self.assertIn("def close_object_stream(stream)", source)
        self.assertIn("stream.close()", source)
        self.assertIn("stream.release_conn()", source)
        self.assertIn("def iter_object_stream(stream", source)
        self.assertIn("finally:\n        close_object_stream(stream)", source)

    def test_all_learn_download_routes_use_the_cleanup_contract(self) -> None:
        route_files = (
            "app/api/lessons.py",
            "app/api/submissions.py",
            "app/api/education.py",
        )
        for relative_path in route_files:
            with self.subTest(relative_path=relative_path):
                source = (SERVICE_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("iter_object_stream(stream)", source)
                self.assertIn("background=BackgroundTask(close_object_stream, stream)", source)
        source = (SERVICE_ROOT / "app/api/challenges.py").read_text(encoding="utf-8")
        self.assertIn("return await resolve_attachment(db, challenge, file_index=file_index)", source)
        source = (SERVICE_ROOT / "app/services/challenge_attachments.py").read_text(encoding="utf-8")
        self.assertIn("iter_object_stream(stream)", source)
        self.assertIn("background=BackgroundTask(close_object_stream, stream)", source)

    def test_competition_route_rechecks_access_and_delegates_the_owned_file(self):
        from app.api import challenges
        challenge, db, response, user = SimpleNamespace(id=17), Mock(), object(), uuid4()
        db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: challenge))
        with patch.object(challenges, "_require_competition_context", AsyncMock()) as access, \
             patch.object(challenges, "resolve_attachment", AsyncMock(return_value=response)) as resolve:
            self.assertIs(immediate(challenges.download_competition_challenge_file(
                "owned-competition", 17, 0, db=db, user_id=user)), response)
        access.assert_awaited_once_with(challenge=challenge, competition_id="owned-competition", user_id=user, purpose="view")
        resolve.assert_awaited_once_with(db, challenge, file_index=0)

    def test_resolver_keeps_background_cleanup_and_iterator_closes_on_disconnect(self):
        from app.services import challenge_attachments as service
        from app.utils.minio_client import iter_object_stream
        stream = Mock()
        stream.read.return_value = b"bytes"
        parent = SimpleNamespace(id=17, files=["challenges/misc/easy/17/owned.bin"])
        with patch.object(service, "run_in_threadpool", AsyncMock(side_effect=[
                SimpleNamespace(size=5), (stream, "application/octet-stream", 5)])):
            response = immediate(service.resolve_attachment(Mock(), parent, file_index=0))
        self.assertIs(response.background.func, service.close_object_stream)
        self.assertEqual(response.background.args, (stream,))
        iterator = iter_object_stream(stream)
        self.assertEqual(next(iterator), b"bytes")
        iterator.close()
        stream.close.assert_called_once()
        stream.release_conn.assert_called_once()


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("pure callback unexpectedly suspended")


if __name__ == "__main__":
    unittest.main()

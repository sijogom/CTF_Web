"""빌드 컨텍스트 업로드의 부분 저장·크기 제한 계약을 고정한다."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BuildContextUploadContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (PROJECT_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        cls.route = cls.source.split("async def upload_build_files(", 1)[1].split(
            "@router.get(\"/challenges/{challenge_id}/build-files\")", 1
        )[0]

    def test_reads_only_one_byte_over_the_per_file_limit(self) -> None:
        self.assertIn("data = await file.read(MAX_BUILD_FILE_SIZE + 1)", self.route)
        self.assertIn("if len(data) > MAX_BUILD_FILE_SIZE:", self.route)

    def test_upload_stream_is_closed_after_read_even_on_read_error(self) -> None:
        read_at = self.route.index("data = await file.read(MAX_BUILD_FILE_SIZE + 1)")
        close_at = self.route.index("await file.close()", read_at)
        self.assertIn("finally:", self.route[read_at:close_at])

    def test_validates_all_files_before_first_storage_write(self) -> None:
        prepared_at = self.route.index("prepared_uploads.append((filename, stored_data))")
        storage_at = self.route.index("for filename, stored_data in prepared_uploads:")
        upload_at = self.route.index("upload_build_file(challenge_id, filename, stored_data)")
        self.assertLess(prepared_at, storage_at)
        self.assertLess(storage_at, upload_at)
        self.assertIn("seen_filenames", self.route)
        self.assertIn("중복된 빌드 파일명입니다", self.route)

    def test_replacing_an_existing_file_does_not_double_count_its_size(self) -> None:
        self.assertIn(
            'existing_size_by_name = {f["name"]: f["size"] for f in existing}',
            self.route,
        )
        self.assertIn("replaced_total = sum(", self.route)
        self.assertIn("final_total = existing_total - replaced_total + new_total", self.route)
        self.assertIn("if final_total > MAX_BUILD_TOTAL_SIZE:", self.route)

    def test_storage_failure_has_best_effort_rollback(self) -> None:
        self.assertIn("부분 빌드 컨텍스트 정리 실패", self.route)
        self.assertIn("delete_build_file(challenge_id, item[\"name\"])", self.route)
        self.assertIn("raise _build_context_storage_unavailable() from error", self.route)

    def test_delete_does_not_remove_the_database_row_when_context_cleanup_is_unavailable(self) -> None:
        delete_route = self.source.split(
            "async def delete_challenge(", 1
        )[1].split(
            "# ──── 빌드 컨텍스트 파일 관리", 1
        )[0]
        self.assertIn("delete_all_build_files(challenge.id)", delete_route)
        self.assertIn("except BuildContextStorageUnavailableError:", delete_route)
        self.assertIn("raise _build_context_storage_unavailable() from None", delete_route)

    def test_single_file_delete_surfaces_storage_failure(self) -> None:
        delete_route = self.source.split(
            "async def remove_build_file(", 1
        )[1]
        self.assertIn("delete_build_file(challenge_id, filename)", delete_route)
        self.assertIn("except BuildContextStorageUnavailableError:", delete_route)
        self.assertIn("raise _build_context_storage_unavailable() from None", delete_route)


if __name__ == "__main__":
    unittest.main()

"""강의·과제 교육 운영 V1의 순수 규칙·인가·구조 계약.

이 테스트는 DB, MinIO, Docker, 실제 Auth 서비스 없이 실행한다. 따라서 통과는
정적/순수 규칙 근거일 뿐 실제 파일 저장·권한 프록시 런타임 검증이 아니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.education import (  # noqa: E402
    EducationResourceLinkCreate,
    TeachingClassCreate,
)
from app.utils.education_visibility import (  # noqa: E402
    can_student_view_grade,
    is_student_assignment_visible,
    is_submission_window_open,
)


EDUCATION_API = SERVICE_ROOT / "app" / "api" / "education.py"
EDUCATION_MODEL = SERVICE_ROOT / "app" / "models" / "education.py"
EDUCATION_SCHEMA = SERVICE_ROOT / "app" / "schemas" / "education.py"
AUTHORIZATION = SERVICE_ROOT / "app" / "utils" / "education_authorization.py"
MINIO_CLIENT = SERVICE_ROOT / "app" / "utils" / "minio_client.py"
CLAMD_SCANNER = SERVICE_ROOT / "app" / "utils" / "clamd_scanner.py"
MAIN = SERVICE_ROOT / "app" / "main.py"
MIGRATION = SERVICE_ROOT / "migrations" / "versions" / "20260813_010_education_domain_v1.py"
CONFIG = SERVICE_ROOT / "app" / "config.py"
CADDYFILE = SERVICE_ROOT.parents[1] / "infra" / "caddy" / "Caddyfile"
PROD_COMPOSE = SERVICE_ROOT.parents[1] / "infra" / "docker-compose.prod.yml"


class EducationVisibilityTests(unittest.TestCase):
    def test_submission_window_requires_active_enrollment_published_states_and_utc_range(self) -> None:
        now = datetime(2026, 8, 13, tzinfo=timezone.utc)
        self.assertTrue(
            is_submission_window_open(
                class_status="published",
                enrollment_status="active",
                assignment_status="published",
                available_from_at=now - timedelta(minutes=1),
                due_at=now + timedelta(minutes=1),
                now=now,
            )
        )
        self.assertFalse(
            is_submission_window_open(
                class_status="published",
                enrollment_status="dropped",
                assignment_status="published",
                available_from_at=None,
                due_at=None,
                now=now,
            )
        )
        self.assertFalse(
            is_submission_window_open(
                class_status="published",
                enrollment_status="active",
                assignment_status="draft",
                available_from_at=None,
                due_at=None,
                now=now,
            )
        )
        self.assertFalse(
            is_submission_window_open(
                class_status="archived",
                enrollment_status="active",
                assignment_status="published",
                available_from_at=None,
                due_at=None,
                now=now,
            )
        )

    def test_student_visibility_never_exposes_draft_grade(self) -> None:
        self.assertTrue(
            is_student_assignment_visible(
                class_status="published",
                enrollment_status="active",
                assignment_status="published",
            )
        )
        self.assertFalse(
            is_student_assignment_visible(
                class_status="published",
                enrollment_status="active",
                assignment_status="draft",
            )
        )
        self.assertTrue(can_student_view_grade(grade_status="published", enrollment_status="active"))
        self.assertFalse(can_student_view_grade(grade_status="draft", enrollment_status="active"))
        self.assertFalse(can_student_view_grade(grade_status="published", enrollment_status="dropped"))

    def test_schema_normalizes_aware_time_and_rejects_naive_time(self) -> None:
        teaching_class = TeachingClassCreate(
            title="강의",
            description_markdown="본문",
            starts_at=datetime(2026, 8, 13, 19, 0, tzinfo=timezone(timedelta(hours=9))),
        )
        self.assertEqual(
            teaching_class.starts_at,
            datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(ValidationError):
            TeachingClassCreate(
                title="강의",
                description_markdown="본문",
                starts_at=datetime(2026, 8, 13, 10, 0),
            )

    def test_schema_rejects_unsafe_markdown_and_wrong_resource_target(self) -> None:
        with self.assertRaises(ValidationError):
            TeachingClassCreate(
                title="강의",
                description_markdown="[위험](javascript:alert(1))",
            )
        with self.assertRaises(ValidationError):
            EducationResourceLinkCreate(resource_type="wargame", course_id=uuid4())


class EducationAuthorizationSourceTests(unittest.TestCase):
    def test_inactive_banned_auth_and_grant_storage_fail_closed_before_authorization(self) -> None:
        source = AUTHORIZATION.read_text(encoding="utf-8")
        self.assertIn("get_current_authorization(str(user_id))", source)
        self.assertIn("if not is_active or global_role not in _GLOBAL_AUTH_ROLES", source)
        self.assertIn("status.HTTP_401_UNAUTHORIZED", source)
        self.assertLess(
            source.index("get_current_authorization(str(user_id))"),
            source.index("select(EducationRoleGrant.id)"),
        )
        self.assertIn("except SQLAlchemyError", source)
        self.assertIn("status.HTTP_503_SERVICE_UNAVAILABLE", source)

    def test_global_admin_and_professor_grant_are_deliberately_different_scopes(self) -> None:
        source = AUTHORIZATION.read_text(encoding="utf-8")
        self.assertIn('_GLOBAL_AUTH_ROLES = frozenset({"user", "moderator", "admin"})', source)
        self.assertIn('return self.global_role == "admin"', source)
        self.assertIn("return self.is_global_admin or self.has_professor_admin_grant", source)
        self.assertIn("전역 관리자 API는 이 모듈을 사용하지", source)


class EducationAssetRulesTests(unittest.TestCase):
    def test_private_asset_prefix_filename_validation_and_global_safety_caps(self) -> None:
        source = MINIO_CLIENT.read_text(encoding="utf-8")
        self.assertIn('EDUCATION_ASSETS_BUCKET = "education-private-assets"', source)
        self.assertIn("MAX_EDUCATION_ASSET_SIZE = 250 * 1024 * 1024", source)
        self.assertIn("MAX_EDUCATION_ASSET_CLASS_TOTAL_SIZE = 2 * 1024 * 1024 * 1024", source)
        self.assertIn("MAX_EDUCATION_ASSETS_PER_CLASS = 200", source)
        self.assertIn("def validate_education_asset_filename", source)
        self.assertIn("if extension not in EDUCATION_ASSET_EXTENSIONS", source)
        self.assertIn("def is_education_asset_object_path", source)
        self.assertIn("_EDUCATION_ASSET_OBJECT_PATH_PATTERN", source)
        self.assertIn("fullmatch(object_path)", source)

    def test_upload_scanner_uses_bounded_clamd_streaming_and_fails_closed(self) -> None:
        minio = MINIO_CLIENT.read_text(encoding="utf-8")
        scanner = CLAMD_SCANNER.read_text(encoding="utf-8")
        api = EDUCATION_API.read_text(encoding="utf-8")
        config = CONFIG.read_text(encoding="utf-8")
        self.assertIn("class EducationAssetScannerUnavailableError", minio)
        self.assertIn("class EducationAssetMalwareDetectedError", minio)
        self.assertIn("def scan_education_asset_before_storage", minio)
        self.assertIn("scan_stream_with_clamd(", minio)
        self.assertIn("class ClamdScannerUnavailableError", scanner)
        self.assertIn("class ClamdMalwareDetectedError", scanner)
        self.assertIn('scanner_socket.sendall(b"zINSTREAM\\x00")', scanner)
        self.assertIn('struct.pack("!I", len(chunk))', scanner)
        self.assertIn("def _read_nul_framed_reply", scanner)
        self.assertIn("def _classify_reply", scanner)
        self.assertIn("not settings.is_production and settings.education_asset_scan_development_bypass", minio)
        self.assertIn('return "development_bypass"', minio)
        self.assertIn("education_asset_scan_development_bypass: bool = False", config)
        self.assertIn('education_asset_scanner_host: str = "clamav"', config)
        self.assertIn("education_asset_scanner_io_timeout_seconds: float = 150.0", config)
        self.assertIn("scan_education_asset_before_storage,", api)
        self.assertIn("await asyncio.to_thread(\n            scan_education_asset_before_storage,", api)
        self.assertIn("except EducationAssetMalwareDetectedError:", api)
        self.assertIn("except EducationAssetScannerUnavailableError:", api)
        self.assertIn("status.HTTP_503_SERVICE_UNAVAILABLE", api)
        self.assertIn("status.HTTP_422_UNPROCESSABLE_ENTITY", api)
        scan_rejection = api[
            api.index("async def _record_education_asset_scan_rejection"):
            api.index("async def _create_education_asset")
        ]
        self.assertIn("class_id: UUID", scan_rejection)
        self.assertIn("assignment_id: UUID | None", scan_rejection)
        self.assertNotIn("klass: TeachingClass", scan_rejection)
        self.assertNotIn("assignment: EducationAssignment", scan_rejection)

    def test_scanner_compose_contract_is_private_bounded_and_health_checked(self) -> None:
        base_compose = (SERVICE_ROOT.parents[1] / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        prod_compose = PROD_COMPOSE.read_text(encoding="utf-8")
        scanner = base_compose.split("  clamav:\n", 1)[1].split("  learn-migrate:\n", 1)[0]
        learn_service = base_compose.split("  learn-service:\n", 1)[1].split("  knowledge-candidate-runner:\n", 1)[0]
        self.assertIn("image: clamav/clamav:1.5", scanner)
        self.assertNotIn("\n    ports:", scanner)
        self.assertIn("- ctf-scanner", scanner)
        self.assertIn('CLAMD_CONF_StreamMaxLength: "250M"', scanner)
        self.assertIn('CLAMD_CONF_AlertExceedsMax: "yes"', scanner)
        self.assertIn("cap_drop:\n      - ALL", scanner)
        self.assertIn('test: ["CMD", "clamdcheck.sh"]', scanner)
        self.assertIn("clamav:\n        condition: service_started", learn_service)
        self.assertIn("- ctf-scanner", learn_service)
        self.assertIn("clamav_data:", base_compose)
        self.assertIn("ctf-scanner:", base_compose)
        self.assertIn("  clamav:\n", prod_compose)
        self.assertIn("memory: 4096M", prod_compose)

    def test_private_download_releases_minio_response_and_upload_does_not_copy_spool(self) -> None:
        minio = MINIO_CLIENT.read_text(encoding="utf-8")
        api = EDUCATION_API.read_text(encoding="utf-8")
        self.assertIn("def close_object_stream", minio)
        self.assertIn("stream.close()", minio)
        self.assertIn("stream.release_conn()", minio)
        self.assertIn("def iter_object_stream", minio)
        self.assertIn("finally:\n        close_object_stream(stream)", minio)
        self.assertIn("iter_object_stream(stream)", api)
        self.assertIn("background=BackgroundTask(close_object_stream, stream)", api)
        self.assertIn("async def _validate_education_upload", api)
        self.assertNotIn("SpooledTemporaryFile", api)
        self.assertIn("data_stream=file.file", api)

    def test_models_keep_existing_course_domain_separate(self) -> None:
        source = EDUCATION_MODEL.read_text(encoding="utf-8")
        self.assertIn('class TeachingClass(Base)', source)
        self.assertIn('class EducationAssignment(Base)', source)
        self.assertIn('class EducationAsset(Base)', source)
        self.assertIn('ForeignKey("courses.id", ondelete="RESTRICT")', source)
        self.assertNotIn("UserProgress", source)
        self.assertNotIn("Competition", source)
        self.assertNotIn("CTFd", source)


class EducationSourceContractTests(unittest.TestCase):
    def test_api_routes_revalidate_auth_and_keep_roles_scoped(self) -> None:
        source = EDUCATION_API.read_text(encoding="utf-8")
        schema = EDUCATION_SCHEMA.read_text(encoding="utf-8")
        authorization = AUTHORIZATION.read_text(encoding="utf-8")
        self.assertIn('APIRouter(prefix="/education"', source)
        self.assertIn('@router.get("/me/permissions", response_model=EducationPermissionsResponse)', source)
        self.assertIn("class EducationPermissionsResponse", schema)
        for field_name in ("user_id", "is_education_manager", "is_platform_admin", "education_role"):
            self.assertIn(field_name, schema)
        self.assertIn("get_education_principal", source)
        self.assertIn("require_global_admin(principal)", source)
        self.assertIn("require_education_manager(principal)", source)
        self.assertIn("get_current_authorization(str(user_id))", authorization)
        self.assertLess(
            authorization.index("get_current_authorization(str(user_id))"),
            authorization.index("select(EducationRoleGrant.id)"),
        )
        self.assertIn("역할은 여기서 변경하지 않는다", authorization)
        self.assertNotIn('require_role("professor_admin")', source)

    def test_resource_links_and_assets_have_server_side_boundaries(self) -> None:
        source = EDUCATION_API.read_text(encoding="utf-8")
        schema = EDUCATION_SCHEMA.read_text(encoding="utf-8")
        self.assertIn('Challenge.challenge_type == "wargame"', source)
        self.assertIn("Course.is_published.is_(True)", source)
        self.assertIn("Course.status == ContentStatus.APPROVED.value", source)
        self.assertIn("is_education_asset_object_path", source)
        self.assertIn('"Content-Disposition"', source)
        self.assertIn('"X-Content-Type-Options": "nosniff"', source)
        self.assertIn('@router.get("/classes/{class_id}/asset-usage", response_model=EducationAssetUsageResponse)', source)
        self.assertIn("class EducationAssetResponse", schema)
        self.assertIn("class EducationAssetUsageResponse", schema)
        response_body = schema[schema.index("class EducationAssetResponse"):schema.index("class EducationAssetListResponse")]
        self.assertNotIn("\n    object_path:", response_body)

    def test_resource_links_expose_only_server_resolved_local_routes(self) -> None:
        source = EDUCATION_API.read_text(encoding="utf-8")
        schema = EDUCATION_SCHEMA.read_text(encoding="utf-8")
        self.assertIn("async def _resolve_public_resource_href", source)
        self.assertIn('return f"/learn/{course_id}"', source)
        self.assertIn('return f"/learn/{course_id}/{link.lesson_id}"', source)
        self.assertIn('return f"/wargame/{challenge_id}"', source)
        self.assertIn("href: str | None = None", schema)
        resolver = source[source.index("async def _resolve_public_resource_href"):source.index("async def _resource_response")]
        self.assertNotIn("http://", resolver)
        self.assertNotIn("https://", resolver)

    def test_audit_contract_does_not_copy_private_text_or_object_path(self) -> None:
        source = EDUCATION_API.read_text(encoding="utf-8")
        self.assertGreater(source.count("_add_audit("), 8)
        self.assertIn(
            '"content_length": rich_content_visible_char_count_for_read(body.content_markdown)',
            source,
        )
        self.assertIn('"feedback_recorded": bool(grade.feedback_markdown)', source)
        self.assertNotIn('"content_markdown": body.content_markdown', source)
        self.assertNotIn('"feedback_markdown": body.feedback_markdown', source)
        self.assertNotIn('"object_path": asset.object_path', source)

    def test_global_admin_can_query_minimal_role_grant_audit_only(self) -> None:
        source = EDUCATION_API.read_text(encoding="utf-8")
        self.assertIn('@router.get("/role-grants/audit-events", response_model=EducationAuditListResponse)', source)
        audit_route = source[source.index('async def list_role_grant_audit_events'):source.index('@router.post("/role-grants"')]
        self.assertIn("require_global_admin(principal)", audit_route)
        self.assertIn("EducationAuditEvent.entity_type == \"role_grant\"", audit_route)
        self.assertIn("subject_id: UUID | None = None", audit_route)
        self.assertNotIn("get_current_authorization", audit_route)
        self.assertNotIn("User", audit_route)

    def test_proxy_and_production_tmpfs_bound_education_uploads_without_touching_other_routes(self) -> None:
        caddy = CADDYFILE.read_text(encoding="utf-8")
        compose = PROD_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("@education_asset_upload", caddy)
        self.assertIn("^/ctf/api/learn/education/(?:classes|assignments)/[^/]+/assets$", caddy)
        self.assertIn("max_size 268435456", caddy)
        self.assertLess(caddy.index("handle @education_asset_upload"), caddy.index("handle /ctf/api/learn/*"))
        self.assertIn("/tmp:rw,nosuid,nodev,noexec,size=640m,mode=1777", compose)
        self.assertIn("memory: 768M", compose)

    def test_migration_is_additive_reversible_and_registered(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8")
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn('down_revision: Union[str, None] = "20260813_009"', migration)
        for table in (
            "education_role_grants",
            "teaching_classes",
            "teaching_class_enrollments",
            "education_assignments",
            "education_resource_links",
            "education_assignment_submissions",
            "education_assignment_grades",
            "education_assets",
            "education_audit_events",
        ):
            self.assertIn(table, migration)
        self.assertIn("자동 추측을 중단", migration)
        self.assertIn("destructive downgrade", migration)
        self.assertIn('_LEGACY_BOOTSTRAP_CORE_TABLES = {"courses", "lessons", "challenges"}', migration)
        self.assertIn("missing_core = _LEGACY_BOOTSTRAP_CORE_TABLES - _table_names()", migration)
        self.assertIn("def _schema_contract_missing", migration)
        self.assertIn("inspector.get_columns(table)", migration)
        self.assertIn("inspector.get_foreign_keys(table)", migration)
        self.assertIn("inspector.get_check_constraints(table)", migration)
        self.assertIn("partial create를 시작하지 않는다", migration)
        self.assertIn("import app.models.education", main)
        self.assertIn("education_router", main)


if __name__ == "__main__":
    unittest.main()

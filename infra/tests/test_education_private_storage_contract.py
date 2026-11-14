"""교육용 비공개 객체 경계의 정적 계약을 확인한다.

이 검사는 환경 값을 읽거나 Docker를 시작하거나 MinIO에 연결하지 않는다. 승인된
Linux runtime 검증에서 실제 bucket policy를 확인하기 전까지 source 수준의
fail-closed 계약을 유지한다.
"""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class EducationPrivateStorageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.caddy = (PROJECT_ROOT / "infra" / "caddy" / "Caddyfile").read_text(
            encoding="utf-8"
        )
        self.ctfctl = (
            PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh"
        ).read_text(encoding="utf-8")

    def test_caddy_denies_private_bucket_before_generic_storage_proxy(self) -> None:
        matcher = (
            "path_regexp education_private_storage "
            "^/ctf/storage/education-private-assets(?:/|$)"
        )
        private_block = "handle @education_private_storage {"
        generic_storage = "handle /ctf/storage/* {"

        self.assertIn(matcher, self.caddy)
        self.assertEqual(self.caddy.count(private_block), 1)
        self.assertEqual(self.caddy.count(generic_storage), 1)
        storage_start = self.caddy.index(generic_storage)
        private_start = self.caddy.index(private_block)
        self.assertLess(storage_start, private_start)
        proxy_start = self.caddy.index(
            "handle {\n                uri strip_prefix /ctf/storage",
            private_start,
        )
        self.assertLess(private_start, proxy_start)
        frontend_start = self.caddy.index("handle {\n            reverse_proxy {$FRONTEND_HOST")
        self.assertLess(storage_start, frontend_start)
        block = self.caddy[
            private_start : proxy_start
        ]
        self.assertIn("respond 404", block)
        self.assertIn("uri strip_prefix /ctf/storage", self.caddy)

    def test_preflight_uses_nonmutating_anonymous_minio_probes(self) -> None:
        probe_start = self.ctfctl.index("ctfctl_minio_anonymous_status_code() {")
        probe_end = self.ctfctl.index("ctfctl_check_secret_references() {")
        probe = self.ctfctl[probe_start:probe_end]
        preflight = self.ctfctl.split("ctfctl_preflight() {", 1)[1].split(
            "ctfctl_backup() {", 1
        )[0]

        self.assertIn("ctfctl_assert_education_private_bucket_anonymous_contract()", probe)
        self.assertIn("--output /dev/null", probe)
        self.assertIn("--write-out '%{http_code}'", probe)
        self.assertIn("--proto '=http'", probe)
        self.assertIn("?list-type=2&max-keys=1", probe)
        self.assertIn("EDUCATION_PRIVATE_STORAGE_ANONYMOUS_DENIED", probe)
        self.assertIn(
            'ctfctl_assert_education_private_bucket_anonymous_contract "$release" "$env_file"',
            preflight,
        )
        self.assertNotIn("delete_bucket_policy", probe)
        self.assertNotIn("set_bucket_policy", probe)
        self.assertNotIn("docker compose", probe)
        self.assertNotIn(" up ", probe)

    def test_release_preflight_and_runtime_changes_require_the_proxy_contract(self) -> None:
        self.assertIn("ctfctl_assert_education_private_storage_contract()", self.ctfctl)
        self.assertIn("EDUCATION_PRIVATE_STORAGE_ORDER_INVALID", self.ctfctl)
        self.assertIn("EDUCATION_PRIVATE_STORAGE_RESPONSE_INVALID", self.ctfctl)

        command_boundaries = (
            ("ctfctl_preflight() {", "ctfctl_backup() {"),
            ("ctfctl_migrate() {", "ctfctl_deploy() {"),
            ("ctfctl_deploy() {", "ctfctl_rollback() {"),
            ("ctfctl_rollback() {", "ctfctl_smoke_test() {"),
        )
        for start, end in command_boundaries:
            body = self.ctfctl.split(start, 1)[1].split(end, 1)[0]
            self.assertIn(
                'ctfctl_assert_education_private_storage_contract "$release"',
                body,
            )


if __name__ == "__main__":
    unittest.main()

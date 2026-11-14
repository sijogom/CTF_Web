"""dynamic_instance 플래그의 비밀 경계와 입력 계약을 DB 없이 검증한다."""

import base64
import sys
from pathlib import Path
import unittest

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVICE_ROOT.parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.challenge import (  # noqa: E402
    ChallengeCreate,
    ChallengeUpdate,
    DynamicFlagPolicyInput,
    FlagCreate,
)
from app.utils.dynamic_flags import (  # noqa: E402
    DYNAMIC_BUILD_FLAG_PLACEHOLDER,
    DYNAMIC_FLAG_RUNTIME_PATH,
    DYNAMIC_SUBMISSION_MARKER,
    DynamicFlagConfigurationError,
    DynamicFlagFormatError,
    build_dynamic_flag,
    dynamic_token_hmac,
    dynamic_token_matches,
    dynamic_policy_configuration_changed,
    generate_dynamic_locator,
    generate_dynamic_token,
    normalize_build_context_file,
    parse_dynamic_flag,
    parse_dynamic_flag_keyring,
    resolve_dynamic_hmac_key,
)
from app.utils.environment_verification import (  # noqa: E402
    dockerfile_healthcheck_depends_on_flag,
)


class DynamicFlagRuleTests(unittest.TestCase):
    def test_idempotent_policy_save_does_not_invalidate_environment(self) -> None:
        current = {
            "enabled": True,
            "delivery": "file",
            "runtime_path": "/flag.txt",
            "ttl_seconds": 3600,
            "hmac_key_version": "v1",
        }
        self.assertFalse(dynamic_policy_configuration_changed(current, dict(current)))
        self.assertTrue(
            dynamic_policy_configuration_changed(
                current,
                dict(current, ttl_seconds=7200),
            )
        )
        self.assertTrue(dynamic_policy_configuration_changed(current, None))

    def test_dynamic_build_flag_upload_is_replaced_by_a_non_secret_placeholder(self) -> None:
        raw = b"FLAG{author_supplied_secret_must_not_be_persisted}"
        stored = normalize_build_context_file("flag.txt", raw, "dynamic_instance")
        self.assertEqual(stored, DYNAMIC_BUILD_FLAG_PLACEHOLDER)
        self.assertNotEqual(stored, raw)
        self.assertNotIn(b"author_supplied_secret", stored)
        self.assertEqual(
            normalize_build_context_file("assets/flag.txt", raw, "dynamic_instance"),
            DYNAMIC_BUILD_FLAG_PLACEHOLDER,
        )
        self.assertEqual(normalize_build_context_file("app.py", raw, "dynamic_instance"), raw)

    def test_all_flag_modes_accept_optional_flag_file_without_persisting_raw_content(self) -> None:
        raw = b"FLAG{author_supplied_secret_must_not_be_persisted}"
        for flag_type in ("static", "regex", None):
            stored = normalize_build_context_file("flag.txt", raw, flag_type)
            self.assertEqual(stored, DYNAMIC_BUILD_FLAG_PLACEHOLDER)
            self.assertNotIn(b"author_supplied_secret", stored)

    def test_flag_dependent_healthcheck_is_detected_without_reading_flag_content(self) -> None:
        gacha_like = """\
HEALTHCHECK --interval=30s --timeout=3s \\
    CMD python -c \"import json; d=json.load(open('/health')); raise SystemExit(0 if d.get('flag') == 'ok' else 1)\"
"""
        ordinary = "HEALTHCHECK CMD wget -qO- http://127.0.0.1:5000/health"
        self.assertTrue(dockerfile_healthcheck_depends_on_flag(gacha_like))
        self.assertFalse(dockerfile_healthcheck_depends_on_flag(ordinary))
        self.assertFalse(dockerfile_healthcheck_depends_on_flag("HEALTHCHECK NONE"))

    def test_static_and_regex_keep_their_existing_contract_without_flag_txt(self) -> None:
        static = ChallengeCreate(name="static", flag_value="FLAG{legacy_static}")
        self.assertEqual(static.flag_type, "static")
        self.assertEqual(static.env_type, "none")

        regex = ChallengeCreate(
            name="regex",
            flag_type="regex",
            flags=[FlagCreate(type="regex", value=r"FLAG\\{[a-z]+\\}")],
        )
        self.assertEqual(regex.flag_type, "regex")
        self.assertFalse(regex.files)

    def test_dynamic_requires_docker_policy_and_rejects_static_material(self) -> None:
        policy = DynamicFlagPolicyInput(hmac_key_version="v1", ttl_seconds=600)
        dynamic = ChallengeCreate(
            name="dynamic",
            flag_type="dynamic_instance",
            env_type="dockerfile",
            dynamic_flag=policy,
        )
        self.assertEqual(dynamic.dynamic_flag.runtime_path, DYNAMIC_FLAG_RUNTIME_PATH)

        with self.assertRaises(ValidationError):
            ChallengeCreate(name="missing-policy", flag_type="dynamic_instance", env_type="image")
        with self.assertRaises(ValidationError):
            ChallengeCreate(
                name="no-container",
                flag_type="dynamic_instance",
                dynamic_flag=policy,
            )
        with self.assertRaises(ValidationError):
            ChallengeCreate(
                name="static-material",
                flag_type="dynamic_instance",
                env_type="image",
                flag_value="FLAG{must_not_be_persisted}",
                dynamic_flag=policy,
            )

    def test_dynamic_policy_is_fixed_to_the_safe_v1_runtime_path(self) -> None:
        with self.assertRaises(ValidationError):
            DynamicFlagPolicyInput(runtime_path="/tmp/flag.txt")
        with self.assertRaises(ValidationError):
            DynamicFlagPolicyInput(ttl_seconds=59)
        with self.assertRaises(ValidationError):
            DynamicFlagPolicyInput(hmac_key_version="not allowed")
        with self.assertRaises(ValidationError):
            DynamicFlagPolicyInput.model_validate({"raw_flag": "FLAG{must_not_be_accepted}"})

    def test_explicit_null_policy_is_distinguishable_from_an_omitted_patch_field(self) -> None:
        omitted = ChallengeUpdate()
        explicit_null = ChallengeUpdate.model_validate({"dynamic_flag": None})
        self.assertNotIn("dynamic_flag", omitted.model_fields_set)
        self.assertIn("dynamic_flag", explicit_null.model_fields_set)

    def test_keyring_is_versioned_and_never_treats_short_key_as_valid(self) -> None:
        encoded = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")
        keyring = parse_dynamic_flag_keyring(f"v1={encoded}")
        self.assertEqual(resolve_dynamic_hmac_key(keyring=keyring, key_version="v1"), b"k" * 32)

        short = base64.urlsafe_b64encode(b"short").decode("ascii").rstrip("=")
        with self.assertRaises(DynamicFlagConfigurationError):
            parse_dynamic_flag_keyring(f"v1={short}")
        with self.assertRaises(DynamicFlagConfigurationError):
            parse_dynamic_flag_keyring(f"v1={encoded}!")
        with self.assertRaises(DynamicFlagConfigurationError):
            resolve_dynamic_hmac_key(keyring=keyring, key_version="missing")
        with self.assertRaises(DynamicFlagConfigurationError):
            dynamic_token_hmac(
                key=b"short",
                locator=generate_dynamic_locator(),
                token=generate_dynamic_token(),
            )

    def test_raw_flag_round_trip_uses_hmac_and_marker_never_contains_the_raw_value(self) -> None:
        key = b"h" * 32
        locator = generate_dynamic_locator()
        token = generate_dynamic_token()
        raw_flag = build_dynamic_flag(locator=locator, token=token)
        parsed = parse_dynamic_flag(raw_flag)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.locator, locator)
        self.assertEqual(parsed.token, token)

        digest = dynamic_token_hmac(key=key, locator=locator, token=token)
        self.assertTrue(
            dynamic_token_matches(
                expected_hmac=digest,
                key=key,
                locator=parsed.locator,
                token=parsed.token,
            )
        )
        self.assertFalse(
            dynamic_token_matches(
                expected_hmac=digest,
                key=key,
                locator=parsed.locator,
                token=generate_dynamic_token(),
            )
        )
        self.assertNotIn(raw_flag, DYNAMIC_SUBMISSION_MARKER)

    def test_model_source_keeps_raw_dynamic_values_out_of_persistence_fields(self) -> None:
        # 이 focused suite는 SQLAlchemy가 없는 읽기 전용 Python에서도 실행해야 한다.
        # ORM metadata의 실제 생성 검증은 의존성이 갖춰진 Learn test 환경에서 별도로
        # 수행한다. 여기서는 source contract가 raw flag/token 필드를 만들지 않는지 확인한다.
        model_source = (SERVICE_ROOT / "app" / "models" / "challenge.py").read_text(encoding="utf-8")
        self.assertIn("token_hmac = Column(LargeBinary(32)", model_source)
        self.assertIn("dynamic_flag_issuance_id = Column(", model_source)
        self.assertNotIn("raw_flag = Column", model_source)
        self.assertNotIn("raw_token = Column", model_source)

    def test_issue_route_rechecks_learn_access_before_issuing_a_secret(self) -> None:
        internal_source = (SERVICE_ROOT / "app" / "api" / "internal.py").read_text(encoding="utf-8")
        access_source = (SERVICE_ROOT / "app" / "utils" / "challenge_access.py").read_text(encoding="utf-8")
        self.assertIn("user_can_access_challenge_for_instance", internal_source)
        self.assertIn("verify_dynamic_flag_internal_key", internal_source)
        self.assertIn('dependencies=[Depends(verify_dynamic_flag_internal_key)]', internal_source)
        self.assertIn("except Exception as error", internal_source)
        self.assertIn("ChallengePrerequisite", access_source)
        self.assertIn("ChallengeLearningPrerequisite", access_source)
        self.assertIn("Submission.competition_id", access_source)

    def test_migration_is_additive_and_records_only_digest_metadata(self) -> None:
        migration_source = (
            SERVICE_ROOT / "migrations" / "versions" / "20260813_005_dynamic_instance_flags.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260813_005"', migration_source)
        self.assertIn('down_revision: Union[str, None] = "20260813_004"', migration_source)
        self.assertIn('"token_hmac"', migration_source)
        self.assertIn('"dynamic_flag_issuance_id"', migration_source)
        self.assertIn("octet_length(token_hmac) = 32", migration_source)
        self.assertIn("CHECK (flag_type IN ('static', 'regex'))", migration_source)
        self.assertNotIn('"raw_flag"', migration_source)
        self.assertNotIn('"raw_token"', migration_source)

    def test_design_keeps_env_runtime_file_contract_outside_learn_persistence(self) -> None:
        design_source = (
            PROJECT_ROOT / "docs" / "design" / "docker-challenge-authoring-and-dynamic-flag-design.md"
        ).read_text(encoding="utf-8")
        self.assertIn("/var/lib/ctf-web/dynamic-flags", design_source)
        self.assertIn("`0700`", design_source)
        self.assertIn("`0444`", design_source)
        self.assertIn("read-only bind mount", design_source)

    def test_admin_detail_and_write_contract_expose_only_policy_metadata(self) -> None:
        challenge_source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(encoding="utf-8")
        for field in (
            '"enabled": policy.enabled',
            '"delivery": policy.delivery',
            '"runtime_path": policy.runtime_path',
            '"ttl_seconds": policy.ttl_seconds',
            '"policy_revision": policy.policy_revision',
            '"hmac_key_version": policy.hmac_key_version',
        ):
            self.assertIn(field, challenge_source)
        self.assertIn("_require_dynamic_flag_feature()", challenge_source)
        self.assertIn("_resolve_dynamic_policy_key_version", challenge_source)
        self.assertIn("_revoke_active_dynamic_issuances_for_stale_environment", challenge_source)
        self.assertIn("dynamic_flag_feature_disabled", challenge_source)

    def test_instance_env_lookup_keeps_static_flag_healthcheck_build_compatible(self) -> None:
        internal_source = (SERVICE_ROOT / "app" / "api" / "internal.py").read_text(
            encoding="utf-8"
        )
        challenge_source = (SERVICE_ROOT / "app" / "api" / "challenges.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Build-context handling supplies a non-secret placeholder", internal_source)
        self.assertNotIn("dynamic_flag_required", internal_source)
        self.assertIn('"flag_type": challenge.flag_type', internal_source)
        self.assertIn("purpose == \"instance\"", internal_source)
        self.assertIn("def _allow_compatibility_flag_healthcheck", challenge_source)
        self.assertGreaterEqual(
            challenge_source.count("_allow_compatibility_flag_healthcheck("), 3
        )
        self.assertIn("normalize_build_context_file", challenge_source)
        self.assertIn("uploaded_dockerfile = data.decode(\"utf-8\")", challenge_source)

    def test_published_docker_build_repair_keeps_tag_and_revision_immutable(self) -> None:
        internal_source = (SERVICE_ROOT / "app" / "api" / "internal.py").read_text(
            encoding="utf-8"
        )
        build_files_section = internal_source.split(
            "async def challenge_build_files", 1
        )[1].split("class DockerImageUpdate", 1)[0]
        self.assertIn("is_env_orchestrator_build_request(request)", build_files_section)
        self.assertIn("X-Env-Orchestrator-Build", internal_source)
        self.assertIn(
            "if body.docker_image != challenge.docker_image:",
            internal_source,
        )
        self.assertIn("published_image_immutable", internal_source)
        self.assertIn("if not challenge.is_published:", internal_source)


if __name__ == "__main__":
    unittest.main()

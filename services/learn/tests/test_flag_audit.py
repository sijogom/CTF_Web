import unittest

from cryptography.fernet import Fernet

from app.utils.flag_audit import (
    FlagStorageUnavailable,
    FlagStorageUnavailable,
    SUBMISSION_REDACTION_MARKER,
    build_challenge_submission_flag_metadata,
    build_submission_list_metadata,
    build_submission_storage_metadata,
    decrypt_submitted_flag,
    decrypt_challenge_submission_flag,
    encrypt_submitted_flag,
    encrypt_challenge_submission_flag,
    merge_stored_submission_audit_metadata,
)


class SubmissionListMetadataTests(unittest.TestCase):
    def test_submitted_flag_ciphertext_round_trips_with_configured_key(self) -> None:
        raw_value = "CTF{submitted-flag-admin-reveal}"
        key = Fernet.generate_key().decode("ascii")

        ciphertext = encrypt_submitted_flag(
            raw_value,
            encryption_key=key,
            fallback_secret=None,
        )

        self.assertNotIn(raw_value.encode("utf-8"), ciphertext)
        self.assertEqual(
            decrypt_submitted_flag(
                ciphertext,
                encryption_key=key,
                fallback_secret=None,
            ),
            raw_value,
        )

    def test_submitted_flag_uses_internal_secret_fallback_without_exposing_it(self) -> None:
        raw_value = "CTF{derived-key-fallback}"
        fallback_secret = "internal-secret-for-unit-test"

        ciphertext = encrypt_submitted_flag(
            raw_value,
            encryption_key=None,
            fallback_secret=fallback_secret,
        )

        self.assertNotIn(raw_value.encode("utf-8"), ciphertext)
        self.assertEqual(
            decrypt_submitted_flag(
                ciphertext,
                encryption_key=None,
                fallback_secret=fallback_secret,
            ),
            raw_value,
        )

    def test_submitted_flag_storage_fails_closed_without_any_key(self) -> None:
        with self.assertRaises(FlagStorageUnavailable):
            encrypt_submitted_flag(
                "CTF{no-key}",
                encryption_key=None,
                fallback_secret=None,
            )

    def test_challenge_submission_flag_round_trips_only_inside_encryption_boundary(self) -> None:
        raw_value = "CTF{encrypted-authoring-flag}"
        key = Fernet.generate_key().decode("ascii")

        ciphertext = encrypt_challenge_submission_flag(raw_value, encryption_key=key)

        self.assertNotIn(raw_value.encode("utf-8"), ciphertext)
        self.assertEqual(
            decrypt_challenge_submission_flag(ciphertext, encryption_key=key),
            raw_value,
        )

    def test_challenge_submission_flag_fails_closed_without_key(self) -> None:
        with self.assertRaises(FlagStorageUnavailable):
            encrypt_challenge_submission_flag("CTF{no-fallback}", encryption_key="")

    def test_challenge_submission_detail_masks_flag_and_storage_path(self) -> None:
        raw_value = "CTF{challenge-authoring-secret}"

        metadata = build_challenge_submission_flag_metadata(
            raw_value,
            fingerprint_secret="unit-test-secret",
        )

        self.assertEqual(metadata["flag_value_masked"], "[redacted]")
        self.assertTrue(metadata["flag_value_recorded"])
        self.assertEqual(metadata["flag_value_length"], len(raw_value))
        self.assertTrue(str(metadata["flag_value_fingerprint"]).startswith("hmac-sha256:"))
        self.assertNotIn(raw_value, metadata.values())

    def test_challenge_submission_empty_flag_has_no_digest(self) -> None:
        metadata = build_challenge_submission_flag_metadata(
            "",
            fingerprint_secret="unit-test-secret",
        )

        self.assertEqual(metadata["flag_value_masked"], None)
        self.assertFalse(metadata["flag_value_recorded"])
        self.assertEqual(metadata["flag_value_length"], 0)
        self.assertIsNone(metadata["flag_value_fingerprint"])

    def test_metadata_never_contains_raw_value(self) -> None:
        raw_value = "CTF{do-not-expose-this-value}"

        metadata = build_submission_list_metadata(raw_value)

        self.assertEqual(
            metadata,
            {
                "submitted_flag_recorded": True,
                "submitted_flag_length": len(raw_value),
            },
        )
        self.assertNotIn(raw_value, metadata.values())

    def test_empty_value_is_not_marked_as_recorded(self) -> None:
        self.assertEqual(
            build_submission_list_metadata(""),
            {"submitted_flag_recorded": False, "submitted_flag_length": 0},
        )

    def test_storage_metadata_never_returns_the_raw_flag(self) -> None:
        raw_value = "CTF{new-storage-contract}"
        metadata = build_submission_storage_metadata(
            raw_value,
            fingerprint_secret="unit-test-secret",
        )

        self.assertEqual(metadata["submitted_flag_marker"], SUBMISSION_REDACTION_MARKER)
        self.assertEqual(metadata["submitted_flag_length"], len(raw_value))
        self.assertIsInstance(metadata["submitted_flag_fingerprint"], bytes)
        self.assertNotIn(raw_value, metadata.values())

    def test_stored_digest_replaces_legacy_fallback_metadata(self) -> None:
        raw_value = "CTF{legacy-fallback-only}"
        digest_metadata = build_submission_storage_metadata(
            raw_value,
            fingerprint_secret="unit-test-secret",
        )
        merged = merge_stored_submission_audit_metadata(
            {"submitted_flag_masked": "[redacted]"},
            submitted_flag_length=digest_metadata["submitted_flag_length"],
            submitted_flag_fingerprint=digest_metadata["submitted_flag_fingerprint"],
            submitted_flag_storage_version=digest_metadata["submitted_flag_storage_version"],
        )

        self.assertEqual(merged["submitted_flag_length"], len(raw_value))
        self.assertTrue(str(merged["submitted_flag_fingerprint"]).startswith("hmac-sha256:"))
        self.assertNotIn(raw_value, merged.values())


if __name__ == "__main__":
    unittest.main()

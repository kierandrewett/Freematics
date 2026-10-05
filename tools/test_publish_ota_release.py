"""Tests for the allowlisted GitHub OTA upload boundary."""

import tempfile
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from package_ota_release import (
    ASSET_NAME,
    SIDECAR_NAME,
    _current_source_commit,
    package_release,
    verify_release_directory,
)
from publish_ota_release import publish


REQUIRED_TESTS = (
    "boot_identity", "sd_journal_readback_replay", "upload_continuity",
    "live_usb_telemetry_capture_ages", "dashboard_disconnect_recording_continues",
    "car_off_60_minute_gate", "motion_cancellation", "first_boot_acceptance", "rollback",
)


class PublishOtaReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.firmware = self.root / "firmware.bin"
        source_commit = _current_source_commit().encode("ascii")
        self.source_commit = source_commit.decode("ascii")
        self.firmware.write_bytes(
            b"FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
            b"FREEMATICS_OTA_RELEASE_BUILD=1"
            b"FREEMATICS_RELEASE_VERSION=1.0.1\0"
            b"FREEMATICS_SOURCE_COMMIT=" + source_commit + b"\0"
            b"[BOOT] Build: test-build-123\0"
            b"token-free-fixture-image"
        )
        self.source_commit_check = patch(
            "package_ota_release._current_source_commit",
            return_value=source_commit.decode("ascii"),
        )
        self.source_commit_check.start()
        self.checkout_guard = patch(
            "package_ota_release._source_commit_matches_checkout", return_value=True
        )
        self.checkout_guard.start()
        self.asset_dir = self.root / "assets"
        package_release(self.firmware, self.asset_dir)
        self.evidence = self.root / "hardware-evidence.json"
        self.write_evidence()

    def evidence_object(self):
        image = self.asset_dir / ASSET_NAME
        return {
            "schema_version": 1,
            "tested_at": "2026-10-05T12:30:00Z",
            "firmware_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "source_commit": self.source_commit,
            "build_id": "test-build-123",
            "device": {"model": "Model B", "flash_bytes": 16 * 1024 * 1024},
            "tests": {name: True for name in REQUIRED_TESTS},
        }

    def write_evidence(self, value=None, mode=0o600):
        self.evidence.write_text(json.dumps(value if value is not None else self.evidence_object()),
                                 encoding="utf-8")
        self.evidence.chmod(mode)

    def tearDown(self):
        self.checkout_guard.stop()
        self.source_commit_check.stop()
        self.temp.cleanup()

    @patch("publish_ota_release.subprocess.run")
    def test_uploads_only_the_verified_allowlisted_pair(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=self.source_commit + "\n"),
            SimpleNamespace(returncode=0, stdout='{"assets":[]}'),
            SimpleNamespace(returncode=0, stdout=self.source_commit + "\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["gh", "api", "repos/kierandrewett/Freematics/commits/v1.0.1", "--jq", ".sha"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["gh", "release", "view", "v1.0.1", "--json", "assets",
             "--repo", "kierandrewett/Freematics"],
        )
        self.assertEqual(
            run.call_args_list[3].args[0],
            [
                "gh", "release", "upload", "v1.0.1",
                run.call_args_list[3].args[0][4],
                run.call_args_list[3].args[0][5],
                "--repo", "kierandrewett/Freematics",
            ],
        )
        self.assertNotEqual(Path(run.call_args_list[3].args[0][4]).parent, self.asset_dir)
        self.assertIs(run.call_args_list[0].kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[0].kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[3].kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[3].kwargs["stdout"], run.call_args_list[3].kwargs["stderr"])

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_to_append_to_a_release_with_existing_assets(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=self.source_commit),
            SimpleNamespace(returncode=0, stdout='{"assets":[{"name":"unexpected.bin"}]}'),
        ]
        with self.assertRaisesRegex(ValueError, "release is not empty"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 2)

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_when_release_asset_inventory_cannot_be_verified(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=self.source_commit),
            SimpleNamespace(returncode=1, stdout=""),
        ]
        with self.assertRaisesRegex(RuntimeError, "could not verify"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 2)

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_tag_that_points_to_different_source(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="0" * 40)
        with self.assertRaisesRegex(ValueError, "does not point to the firmware"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 1)

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_when_tag_source_cannot_be_verified(self, run):
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        with self.assertRaisesRegex(RuntimeError, "could not verify the source commit"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 1)

    @patch("publish_ota_release.subprocess.run")
    def test_rejects_invalid_tag_before_upload(self, run):
        with self.assertRaisesRegex(ValueError, "tag has an invalid format"):
            publish("--clobber", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_rejects_tag_that_does_not_match_firmware_version(self, run):
        with self.assertRaisesRegex(ValueError, "does not match the firmware"):
            publish("v1.0.2", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_revalidates_assets_immediately_before_upload(self, run):
        (self.asset_dir / "private.log").write_text("not for release", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly the approved"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_tag_moved_after_release_metadata_check(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=self.source_commit),
            SimpleNamespace(returncode=0, stdout='{"assets":[]}'),
            SimpleNamespace(returncode=0, stdout="0" * 40),
        ]
        with self.assertRaisesRegex(ValueError, "tag changed during publication"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 3)

    @patch("publish_ota_release.subprocess.run")
    def test_uploads_private_snapshot_if_inputs_change_after_snapshot(self, run):
        expected_image = (self.asset_dir / ASSET_NAME).read_bytes()
        expected_sidecar = (self.asset_dir / SIDECAR_NAME).read_bytes()
        uploaded = {}
        tag_checks = 0

        def mutate_inputs_at_final_tag_check(args, **kwargs):
            if args[:2] == ["gh", "api"] and args[2] == "repos/kierandrewett/Freematics/commits/v1.0.1":
                nonlocal tag_checks
                tag_checks += 1
                if tag_checks == 2:
                    (self.asset_dir / ASSET_NAME).write_bytes(b"changed after validation")
                    (self.asset_dir / SIDECAR_NAME).write_bytes(b"changed after validation")
                return SimpleNamespace(returncode=0, stdout=self.source_commit)
            if args[:3] == ["gh", "release", "view"]:
                return SimpleNamespace(returncode=0, stdout='{"assets":[]}')
            if args[:3] == ["gh", "release", "upload"]:
                uploaded["image"] = Path(args[4]).read_bytes()
                uploaded["sidecar"] = Path(args[5]).read_bytes()
                return SimpleNamespace(returncode=0, stdout="")
            raise AssertionError(f"unexpected external command: {args!r}")

        run.side_effect = mutate_inputs_at_final_tag_check
        publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(uploaded, {"image": expected_image, "sidecar": expected_sidecar})

    @patch("publish_ota_release.subprocess.run")
    def test_valid_hardware_evidence_allows_publication(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout=self.source_commit),
            SimpleNamespace(returncode=0, stdout='{"assets":[]}'),
            SimpleNamespace(returncode=0, stdout=self.source_commit),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        publish("v1.0.1", self.asset_dir, self.evidence)
        self.assertEqual(run.call_count, 4)

    @patch("publish_ota_release.subprocess.run")
    def test_missing_or_unknown_top_level_and_nested_fields_fail_before_gh(self, run):
        variants = []
        base = self.evidence_object()
        for field in base:
            value = dict(base)
            del value[field]
            variants.append(value)
        value = dict(base)
        value["extra"] = "unexpected"
        variants.append(value)
        for parent, key in (("device", "extra"), ("tests", "extra")):
            value = self.evidence_object()
            value[parent] = dict(value[parent])
            value[parent][key] = True
            variants.append(value)
        for field in ("model", "flash_bytes"):
            value = self.evidence_object()
            del value["device"][field]
            variants.append(value)
        for value in variants:
            with self.subTest(fields=tuple(value)):
                self.write_evidence(value)
                with self.assertRaises(ValueError):
                    publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_every_required_hardware_test_must_be_true(self, run):
        for name in REQUIRED_TESTS:
            value = self.evidence_object()
            del value["tests"][name]
            self.write_evidence(value)
            with self.subTest(missing=name), self.assertRaises(ValueError):
                publish("v1.0.1", self.asset_dir, self.evidence)
            value = self.evidence_object()
            value["tests"][name] = False
            self.write_evidence(value)
            with self.subTest(false=name), self.assertRaisesRegex(ValueError, "failed hardware"):
                publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_mismatched_image_commit_build_and_device_are_rejected_before_gh(self, run):
        cases = [
            ("firmware_sha256", "0" * 64),
            ("source_commit", "0" * 40),
            ("build_id", "other-build"),
        ]
        for key, value in cases:
            evidence = self.evidence_object()
            evidence[key] = value
            self.write_evidence(evidence)
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish("v1.0.1", self.asset_dir, self.evidence)
        for key, value in (("model", "Model C"), ("flash_bytes", 4 * 1024 * 1024)):
            evidence = self.evidence_object()
            evidence["device"][key] = value
            self.write_evidence(evidence)
            with self.subTest(device_key=key), self.assertRaises(ValueError):
                publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_bad_timestamp_schema_or_private_file_metadata_is_rejected(self, run):
        for timestamp in ("", "2026-02-30T12:00:00Z", "2026-10-05T12:00:00+00:00", "yesterday"):
            evidence = self.evidence_object()
            evidence["tested_at"] = timestamp
            self.write_evidence(evidence)
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                publish("v1.0.1", self.asset_dir, self.evidence)
        evidence = self.evidence_object()
        evidence["schema_version"] = 2
        self.write_evidence(evidence)
        with self.assertRaisesRegex(ValueError, "schema version"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.write_evidence(mode=0o640)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    @patch("publish_ota_release.subprocess.run")
    def test_symlink_evidence_is_rejected_before_gh(self, run):
        link = self.root / "evidence-link.json"
        link.symlink_to(self.evidence)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            publish("v1.0.1", self.asset_dir, link)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_malformed_json_is_rejected_before_gh(self, run):
        self.evidence.write_text('{"schema_version":', encoding="utf-8")
        self.evidence.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        self.evidence.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        self.evidence.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "duplicate JSON keys"):
            publish("v1.0.1", self.asset_dir, self.evidence)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

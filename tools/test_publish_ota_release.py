"""Tests for the allowlisted GitHub OTA upload boundary."""

import tempfile
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
)
from publish_ota_release import publish


class PublishOtaReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.firmware = self.root / "firmware.bin"
        source_commit = _current_source_commit().encode("ascii")
        self.firmware.write_bytes(
            b"FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
            b"FREEMATICS_OTA_RELEASE_BUILD=1"
            b"FREEMATICS_RELEASE_VERSION=1.0.1\0"
            b"FREEMATICS_SOURCE_COMMIT=" + source_commit + b"\0"
            b"token-free-fixture-image"
        )
        self.source_commit_check = patch(
            "package_ota_release._current_source_commit",
            return_value=source_commit.decode("ascii"),
        )
        self.source_commit_check.start()
        self.asset_dir = self.root / "assets"
        package_release(self.firmware, self.asset_dir)

    def tearDown(self):
        self.source_commit_check.stop()
        self.temp.cleanup()

    @patch("publish_ota_release.subprocess.run")
    def test_uploads_only_the_verified_allowlisted_pair(self, run):
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout='{"assets":[]}'),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        publish("v1.0.1", self.asset_dir)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["gh", "release", "view", "v1.0.1", "--json", "assets",
             "--repo", "kierandrewett/Freematics"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "gh", "release", "upload", "v1.0.1",
                str(self.asset_dir / ASSET_NAME),
                str(self.asset_dir / SIDECAR_NAME),
                "--repo", "kierandrewett/Freematics",
            ],
        )
        self.assertIs(run.call_args_list[0].kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[0].kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[1].kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(run.call_args_list[1].kwargs["stdout"], run.call_args_list[1].kwargs["stderr"])

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_to_append_to_a_release_with_existing_assets(self, run):
        run.return_value = SimpleNamespace(
            returncode=0, stdout='{"assets":[{"name":"unexpected.bin"}]}'
        )
        with self.assertRaisesRegex(ValueError, "release is not empty"):
            publish("v1.0.1", self.asset_dir)
        self.assertEqual(run.call_count, 1)

    @patch("publish_ota_release.subprocess.run")
    def test_refuses_when_release_asset_inventory_cannot_be_verified(self, run):
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        with self.assertRaisesRegex(RuntimeError, "could not verify"):
            publish("v1.0.1", self.asset_dir)
        self.assertEqual(run.call_count, 1)

    @patch("publish_ota_release.subprocess.run")
    def test_rejects_invalid_tag_before_upload(self, run):
        with self.assertRaisesRegex(ValueError, "tag has an invalid format"):
            publish("--clobber", self.asset_dir)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_rejects_tag_that_does_not_match_firmware_version(self, run):
        with self.assertRaisesRegex(ValueError, "does not match the firmware"):
            publish("v1.0.2", self.asset_dir)
        run.assert_not_called()

    @patch("publish_ota_release.subprocess.run")
    def test_revalidates_assets_immediately_before_upload(self, run):
        (self.asset_dir / "private.log").write_text("not for release", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly the approved"):
            publish("v1.0.1", self.asset_dir)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

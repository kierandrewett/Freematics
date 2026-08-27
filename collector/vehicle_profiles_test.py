from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collector.vehicle_profiles import (
    ProfileValidationError,
    VehicleIdentity,
    load_profile,
    permitted_candidates,
    profile_matches,
)


PROFILE_PATH = Path(__file__).resolve().parents[1] / "protocols/manufacturers/opel-vauxhall/corsa-d/2006-2014/profile.json"


def current_identity(**overrides: object) -> VehicleIdentity:
    values: dict[str, object] = {
        "manufacturer": "Opel/Vauxhall",
        "model": "Corsa D",
        "year": 2010,
        "vin": "W0L0SDL08A6000001",
        "engine_code": "A12XER",
        "ecu_address": "0x7E0",
    }
    values.update(overrides)
    return VehicleIdentity(**values)


class VehicleProfileRegistryTests(unittest.TestCase):
    def test_current_profile_loads_with_separate_standard_and_experimental_records(self) -> None:
        profile = load_profile(PROFILE_PATH)

        self.assertEqual(profile.identity.manufacturer, "Opel/Vauxhall")
        self.assertEqual(profile.identity.model_years, (2006, 2014))
        self.assertEqual(len(profile.standard_services), 5)
        self.assertEqual(len(profile.manufacturer_candidates), 3)
        self.assertEqual(profile.manufacturer_candidates[0].evidence, None)
        self.assertIn("OBD wiring diagram", profile.connector_buses[0].evidence)
        self.assertTrue(profile.activation.requires_capture_of_raw_response)

    def test_incomplete_identity_does_not_make_profile_usable_or_release_candidates(self) -> None:
        profile = load_profile(PROFILE_PATH)

        identity = current_identity(ecu_address=None)

        self.assertFalse(profile_matches(profile, identity))
        self.assertEqual(permitted_candidates(profile, identity), ())

    def test_complete_identity_releases_only_read_only_identity_gated_metadata(self) -> None:
        profile = load_profile(PROFILE_PATH)

        candidates = permitted_candidates(profile, current_identity())

        self.assertEqual([candidate.name for candidate in candidates], ["vin", "ecu_odometer", "read_data_by_identifier_family"])
        self.assertTrue(all(candidate.read_only for candidate in candidates))
        self.assertTrue(all(candidate.identity_gated for candidate in candidates))
        self.assertFalse(any(callable(value) for candidate in candidates for value in vars(candidate).values()))

    def test_year_mismatch_rejects_profile(self) -> None:
        profile = load_profile(PROFILE_PATH)

        self.assertFalse(profile_matches(profile, current_identity(year=2015)))
        self.assertEqual(permitted_candidates(profile, current_identity(year=2015)), ())

    def test_unsafe_profile_and_candidate_are_rejected(self) -> None:
        source = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        source["safety"]["writes_allowed"] = True

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe.json"
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaises(ProfileValidationError):
                load_profile(path)

        source = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        source["gmlan_candidates"][0]["read_only"] = False

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe-candidate.json"
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaises(ProfileValidationError):
                load_profile(path)

        source = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        source["gmlan_candidates"][0]["service"] = "0x2E"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe-service.json"
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaises(ProfileValidationError):
                load_profile(path)

    def test_unknown_identifier_policy_is_record_and_stop(self) -> None:
        profile = load_profile(PROFILE_PATH)
        candidates = permitted_candidates(profile, current_identity())

        self.assertEqual(profile.activation.unknown_identifier_action, "record_and_stop")
        self.assertEqual(candidates[2].identifier, None)
        self.assertEqual(candidates[2].unknown_identifier_action, "record_and_stop")

    def test_malformed_service_syntax_is_rejected(self) -> None:
        source = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        source["standard_services"][0]["service"] = "service-01"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.json"
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaises(ProfileValidationError):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()

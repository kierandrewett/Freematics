from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from vehicle_profiles import VehicleIdentity, load_profile, permitted_candidates
from vendor_diagnostics import decode_ascii, parse_response


PROFILE_PATH = Path(__file__).resolve().parents[1] / "protocols/manufacturers/opel-vauxhall/corsa-d/2006-2014/profile.json"


class VendorDiagnosticResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profile = load_profile(PROFILE_PATH)
        profile = replace(
            profile,
            manufacturer_candidates=tuple(
                replace(candidate, enabled=True, evidence="test response", provenance="test fixture")
                for candidate in profile.manufacturer_candidates
            ),
        )
        identity = VehicleIdentity(
            manufacturer="Opel/Vauxhall",
            model="Corsa D",
            year=2012,
            vin="W0L00000000000000",
            engine_code="A12XER",
            ecu_address="0x7E0",
        )
        cls.candidates = permitted_candidates(profile, identity)

    def test_gmlan_positive_response_keeps_raw_data_and_decodes_ascii(self) -> None:
        candidate = self.candidates[0]
        observation = parse_response(candidate, b"5A 90 56 49 4E 31 32 33")
        self.assertEqual(observation.status, "positive")
        self.assertEqual(observation.data_hex, "56494E313233")
        self.assertEqual(decode_ascii(observation), "VIN123")

    def test_uds_positive_response_requires_identifier_echo(self) -> None:
        candidate = replace(self.candidates[2], service="0x22", identifier="0xF190")
        observation = parse_response(candidate, "62 F1 90 56 49 4E")
        self.assertEqual(observation.status, "positive")
        self.assertEqual(observation.data_hex, "56494E")

        wrong_identifier = parse_response(candidate, "62 F1 91 56 49 4E")
        self.assertEqual(wrong_identifier.status, "no_positive_response")

    def test_negative_response_records_code_and_backoff_policy(self) -> None:
        observation = parse_response(self.candidates[1], "7F 1A 31")
        self.assertEqual(observation.status, "negative")
        self.assertEqual(observation.negative_code, 0x31)
        self.assertEqual(observation.reason, "record_capability_and_backoff")

    def test_identifier_family_candidate_stops_without_guessing(self) -> None:
        observation = parse_response(self.candidates[2], "62 F1 90 01")
        self.assertEqual(observation.status, "unsupported_identifier")
        self.assertEqual(observation.reason, "record_and_stop")

    def test_policy_rejects_non_read_only_or_disabled_candidate(self) -> None:
        for candidate in (
            replace(self.candidates[0], read_only=False),
            replace(self.candidates[0], enabled=False),
        ):
            observation = parse_response(candidate, "5A 90 01")
            self.assertEqual(observation.status, "policy_rejected")
            self.assertIsNone(observation.data_hex)

    def test_malformed_or_empty_responses_do_not_create_values(self) -> None:
        observation = parse_response(self.candidates[0], "NO DATA")
        self.assertEqual(observation.status, "no_positive_response")
        self.assertIsNone(decode_ascii(observation))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from gmlan_capture import (
    CanFrame,
    Capture,
    CaptureInputError,
    ProfileCandidate,
    _candidate_for,
    _decode_message,
    analyse_capture,
    load_profile_candidates,
    parse_archive,
    parse_jsonl,
)


PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "protocols/manufacturers/opel-vauxhall/corsa-d/2006-2014/profile.json"
)


def archive_record(timestamp_ms: int, monitor_line: str) -> str:
    encoded = monitor_line.encode("ascii").hex().upper()
    return f"0:{timestamp_ms},92:{encoded},"


class CaptureInputTests(unittest.TestCase):
    def test_empty_reassembled_payload_is_ignored(self) -> None:
        class EmptyMessage:
            data = b""

        self.assertIsNone(_decode_message(EmptyMessage(), (), object()))

    def test_exact_candidate_beats_family_fallback(self) -> None:
        family = ProfileCandidate("family", 0x22, None, False)
        exact = ProfileCandidate("specific", 0x22, 0xF190, False)

        self.assertIs(_candidate_for((family, exact), 0x22, 0xF190), exact)

    def test_archive_decodes_hex_encoded_monitor_lines(self) -> None:
        raw = archive_record(100, "5E8 03 5A 90 56 49 4E 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        self.assertEqual(capture.unparsed_records, 0)
        self.assertEqual(len(capture.frames), 1)
        self.assertEqual(capture.frames[0].timestamp_ms, 100)
        self.assertEqual(capture.frames[0].can_id, 0x5E8)
        self.assertEqual(capture.frames[0].data, bytes.fromhex("03 5A 90 56 49 4E 00 00"))

    def test_archive_keeps_unrecognised_monitor_lines_out_of_frame_stream(self) -> None:
        raw = archive_record(100, "NO DATA") + archive_record(200, "7E8 03 62 F1 90 00 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        self.assertEqual(capture.unparsed_records, 1)
        self.assertEqual(len(capture.frames), 1)
        self.assertEqual(capture.frames[0].timestamp_ms, 200)

    def test_archive_counts_records_rejected_by_history_parser(self) -> None:
        raw = archive_record(0xFFFFFFFF, "5E8 03 5A 90 00 00 00 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        self.assertEqual(capture.frames, ())
        self.assertEqual(capture.unparsed_records, 1)

    def test_jsonl_requires_the_declared_capture_shape(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as source:
            source.write(json.dumps({"timestamp_ms": 10, "can_id": "0x7E8", "data_hex": "03 5A 90 00"}) + "\n")
            source.flush()
            capture = parse_jsonl(Path(source.name))

        self.assertEqual(capture.unparsed_records, 0)
        self.assertEqual(capture.frames[0].can_id, 0x7E8)
        self.assertEqual(capture.frames[0].data, bytes.fromhex("03 5A 90 00"))

    def test_jsonl_rejects_unknown_fields(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as source:
            source.write(json.dumps({"timestamp_ms": 10, "can_id": 0x7E8, "data_hex": "03", "secret": "x"}) + "\n")
            source.flush()
            with self.assertRaises(CaptureInputError):
                parse_jsonl(Path(source.name))

    def test_jsonl_rejects_unbounded_timestamp(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as source:
            source.write(
                json.dumps({"timestamp_ms": 10**100, "can_id": "0x7E8", "data_hex": "03 5A 90 00"}) + "\n"
            )
            source.flush()
            with self.assertRaises(CaptureInputError):
                parse_jsonl(Path(source.name))
    def test_profile_rejects_non_list_candidate_data(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as profile:
            profile.write(json.dumps({"gmlan_candidates": None}))
            profile.flush()
            with self.assertRaises(CaptureInputError):
                load_profile_candidates(Path(profile.name))

    def test_profile_defaults_omitted_enabled_to_false(self) -> None:
        profile_data = {"gmlan_candidates": [{"name": "family", "service": "0x22"}]}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as profile:
            profile.write(json.dumps(profile_data))
            profile.flush()
            candidates = load_profile_candidates(Path(profile.name))

        self.assertFalse(candidates[0].enabled)



@unittest.skipUnless(importlib.util.find_spec("scapy"), "Scapy is required for decoder integration tests")
class ScapyAdapterTests(unittest.TestCase):
    def test_positive_response_is_evidence_only_and_matches_disabled_candidate(self) -> None:
        raw = archive_record(100, "5E8 07 5A 90 56 49 4E 31 32")
        raw += archive_record(200, "5E8 10 0B 5A 92 4D 41 52 45")
        raw += archive_record(201, "5E8 21 4C 30 31 30 30 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        report = analyse_capture(capture, load_profile_candidates(PROFILE_PATH))

        self.assertEqual(report["capture"]["frame_count"], 3)
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual(report["observations"][0]["candidate"], "vin")
        self.assertFalse(report["observations"][0]["candidate_enabled"])
        self.assertEqual(report["observations"][0]["capability"], "positive_response_observed")
        self.assertEqual(report["observations"][0]["data_hex"], "56494E3132")
        self.assertEqual(report["observations"][1]["candidate"], "system_supplier_id")
        self.assertEqual(report["observations"][1]["data_hex"], "4D4152454C30313030")

    def test_uds_family_candidate_is_retained_without_guessing_identifier(self) -> None:
        raw = archive_record(300, "7E8 06 62 F1 90 56 49 4E 00")
        raw += archive_record(301, "7E8 03 7F 22 31 00 00 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        report = analyse_capture(capture, load_profile_candidates(PROFILE_PATH))

        self.assertEqual(report["observations"][0]["candidate"], "read_data_by_identifier_family")
        self.assertFalse(report["observations"][0]["candidate_enabled"])
        self.assertEqual(report["observations"][0]["data_hex"], "56494E")
        self.assertEqual(report["observations"][1]["candidate"], "read_data_by_identifier_family")
        self.assertEqual(report["observations"][1]["negative_code"], "0x31")

    def test_truncated_identifier_envelopes_are_reported(self) -> None:
        raw = archive_record(400, "7E8 02 62 F1 00 00 00 00 00")
        raw += archive_record(401, "7E8 02 22 F1 00 00 00 00 00")
        with tempfile.NamedTemporaryFile("w", encoding="ascii") as archive:
            archive.write(raw)
            archive.flush()
            capture = parse_archive(Path(archive.name))

        report = analyse_capture(capture, load_profile_candidates(PROFILE_PATH))

        self.assertEqual([item["status"] for item in report["observations"]], ["malformed", "malformed"])

    def test_incomplete_isotp_messages_are_bounded(self) -> None:
        frames = tuple(
            CanFrame(index, 0x100 + index, bytes.fromhex("10 10 00 00 00 00 00 00"))
            for index in range(1_025)
        )
        capture = Capture("jsonl", "0" * 64, frames, 0)

        with self.assertRaises(CaptureInputError):
            analyse_capture(capture, load_profile_candidates(PROFILE_PATH))


if __name__ == "__main__":
    unittest.main()

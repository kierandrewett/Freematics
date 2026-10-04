"""Regression tests for refusing the wrong USB serial adapter."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from measure_usb_stream import (
    capture_is_newer,
    identify_freematics_usb,
    observe_successful_pid_updates,
    sd_core_error_categories,
    summarize,
    timeout_counter_delta,
)


class FreematicsUsbIdentityTests(unittest.TestCase):
    def make_sysfs(self, root: Path, vendor: str, product: str) -> Path:
        usb_device = root / "devices" / "usb1" / "1-2"
        usb_device.mkdir(parents=True)
        (usb_device / "idVendor").write_text(vendor, encoding="ascii")
        (usb_device / "idProduct").write_text(product, encoding="ascii")
        tty_device = usb_device / "1-2:1.0" / "ttyUSB0"
        tty_device.mkdir(parents=True)
        class_entry = root / "class" / "tty" / "ttyUSB0"
        class_entry.mkdir(parents=True)
        (class_entry / "device").symlink_to(tty_device)
        return root / "class" / "tty"

    def test_accepts_freematics_model_b_cp210x_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_class_tty = self.make_sysfs(root, "10c4", "ea60")
            self.assertEqual(
                identify_freematics_usb("/dev/ttyUSB0", sys_class_tty),
                ("10c4", "ea60"),
            )

    def test_rejects_generic_ch340_reader_before_opening_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys_class_tty = self.make_sysfs(root, "1a86", "7523")
            with patch("measure_usb_stream.os.open") as open_port:
                with self.assertRaisesRegex(ValueError, "expected.*10c4:ea60"):
                    summarize("/dev/ttyUSB0", 5, sys_class_tty)
                open_port.assert_not_called()

    def test_rejects_serial_port_without_verifiable_usb_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "cannot verify USB identity"):
                identify_freematics_usb("/dev/ttyUSB0", Path(temp))


class FreematicsObdTimingTests(unittest.TestCase):
    def test_age_reset_detects_success_even_when_pid_value_is_unchanged(self):
        previous_age = {}
        self.assertEqual(
            observe_successful_pid_updates({0x40C: [20]}, previous_age), []
        )
        self.assertEqual(
            observe_successful_pid_updates({0x40C: [270]}, previous_age), []
        )
        self.assertEqual(
            observe_successful_pid_updates({0x40C: [15]}, previous_age),
            [(0x0C, 15)],
        )

    def test_timeout_counter_delta_handles_uint32_wrap(self):
        self.assertEqual(timeout_counter_delta(None, 3), 0)
        self.assertEqual(timeout_counter_delta(10, 13), 3)
        self.assertEqual(timeout_counter_delta(0xFFFFFFFF, 1), 2)

    def test_capture_order_rejects_replays_and_accepts_u32_wrap(self):
        self.assertTrue(capture_is_newer(None, 7))
        self.assertTrue(capture_is_newer(100, 101))
        self.assertFalse(capture_is_newer(100, 100))
        self.assertFalse(capture_is_newer(100, 99))
        self.assertTrue(capture_is_newer(0xFFFFFFF0, 12))

    def test_sd_core_errors_are_reduced_to_safe_categories(self):
        self.assertEqual(
            sd_core_error_categories(
                b"E (123) sdSelectCard(): Select Failed after 500 ms ready wait"
            ),
            ("card_select_timeout",),
        )
        self.assertEqual(
            sd_core_error_categories(
                b"E (124) f_mount failed: physical drive cannot work"
            ),
            ("fatfs_mount_failure", "physical_drive_not_ready"),
        )
        self.assertEqual(
            sd_core_error_categories(b"[QUEUE] SD journal ready"), ()
        )


if __name__ == "__main__":
    unittest.main()

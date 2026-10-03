"""Regression tests for refusing the wrong USB serial adapter."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from measure_usb_stream import identify_freematics_usb, summarize


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

    def test_accepts_freematics_model_b_cp2104(self):
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


if __name__ == "__main__":
    unittest.main()

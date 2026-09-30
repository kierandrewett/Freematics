from pathlib import Path
import tempfile
import unittest
from production_config import load_build_environment

class BuildEnvironmentTests(unittest.TestCase):

    def test_missing_file_keeps_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_build_environment(Path(directory) / '.env', {'PRODUCTION_BUILD': '1'}), {'PRODUCTION_BUILD': '1'})

    def test_local_values_and_quotes_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('# Local only\nPRODUCTION_BUILD=1\nFREEMATICS_TOKEN="' + 'a' * 64 + '"\n')
            self.assertEqual(load_build_environment(path, {}), {'PRODUCTION_BUILD': '1', 'FREEMATICS_TOKEN': 'a' * 64})

    def test_process_values_override_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('FREEMATICS_TOKEN=local\n')
            process = {'FREEMATICS_TOKEN': 'process'}
            self.assertEqual(load_build_environment(path, process), process)
            self.assertEqual(process, {'FREEMATICS_TOKEN': 'process'})

    def test_shell_text_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('FREEMATICS_TOKEN=$(false)\n')
            self.assertEqual(load_build_environment(path, {})['FREEMATICS_TOKEN'], '$(false)')

    def test_malformed_input_does_not_disclose_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('FREEMATICS_TOKEN secret-value\n')
            with self.assertRaises(ValueError) as error:
                load_build_environment(path, {})
            self.assertNotIn('secret-value', str(error.exception))

    def test_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('PRODUCTION_BUILD=1\nPRODUCTION_BUILD=0\n')
            with self.assertRaises(ValueError):
                load_build_environment(path, {})

    def test_card_formatting_cannot_be_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('FREEMATICS_FORMAT_SD_ONCE=1\n')
            with self.assertRaises(ValueError):
                load_build_environment(path, {})

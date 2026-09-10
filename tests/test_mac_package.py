from pathlib import Path
import importlib.util
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_mac', ROOT / 'tools/package_mac.py')
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class MacPackageTests(unittest.TestCase):
    def test_archive_is_reproducible_and_excludes_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'source'
            for name in ('packaging', 'drivedrop', 'docs'):
                shutil.copytree(ROOT / name, root / name)
            private = root / 'packaging/mac/Contents/Resources/data-employee'
            private.mkdir()
            (private / 'secret.json').write_text('PRIVATE_SENTINEL')
            first, second = Path(directory) / 'a.zip', Path(directory) / 'b.zip'
            self.assertEqual(packager.package(root, first), packager.package(root, second))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(len(archive.namelist()), 13)
                self.assertFalse(any('data-employee' in p for p in archive.namelist()))
                launch = archive.getinfo('DriveDrop Employee.app/Contents/MacOS/DriveDrop')
                self.assertTrue(launch.external_attr >> 16 & 0o111)
                self.assertNotIn(b'\r\n', archive.read(launch))
                self.assertIn('DriveDrop Employee.app/Contents/Resources/payload/drivedrop/inventory.py', archive.namelist())

    def test_mismatched_bundle_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'packaging', root / 'packaging')
            (root / 'drivedrop').mkdir()
            (root / 'drivedrop/__init__.py').write_text('__version__ = "99.0.0"\n')
            with self.assertRaisesRegex(ValueError, 'version differs'):
                packager.package(root, root / 'result.zip')


if __name__ == '__main__':
    unittest.main()

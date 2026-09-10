"""Build the Python Mac launcher ZIP from an explicit source allowlist.

Runs on Windows or macOS; does not claim to sign or test a native macOS app.
"""
from pathlib import Path
import argparse
import hashlib
import plistlib
import re
import zipfile

MODULES = ('__init__', 'client', 'common', 'mac_background', 'inventory')
WRAPPER = ('Info.plist', 'PkgInfo', 'MacOS/DriveDrop',
           'Resources/bootstrap.py', 'Resources/start_employee.py')
GUIDES = ('HUONG-DAN-CAI-MAC-VA-KET-NOI.txt', 'HUONG-DAN-BAO-CAO-0.5.md',
          'MAC-DONG-GOI-VA-BAN-GIAO.md')


def package(root, destination):
    root, destination = Path(root), Path(destination)
    version = re.fullmatch(r'__version__\s*=\s*"([0-9.]+)"\s*',
                           (root / 'drivedrop/__init__.py').read_text(encoding='utf-8')).group(1)
    prefix = 'DriveDrop Employee.app/Contents/'
    entries = {}
    for relative in WRAPPER:
        source = root / 'packaging/mac/Contents' / relative
        if source.is_symlink():
            raise ValueError('Wrapper source cannot be a symlink: ' + relative)
        data = source.read_bytes()
        if relative == 'Info.plist':
            info = plistlib.loads(data)
            if info['CFBundleShortVersionString'] != version:
                raise ValueError('Mac bundle version differs from client version')
        if relative.endswith('.py'):
            compile(data, relative, 'exec')
        entries[prefix + relative] = data.replace(b'\r\n', b'\n')
    for module in MODULES:
        source = root / 'drivedrop' / (module + '.py')
        if source.is_symlink():
            raise ValueError('Client source cannot be a symlink: ' + module)
        data = source.read_bytes()
        compile(data, str(source), 'exec')
        entries[prefix + 'Resources/payload/drivedrop/' + module + '.py'] = data.replace(b'\r\n', b'\n')
    for guide in GUIDES:
        entries[guide] = (root / 'docs' / guide).read_bytes().replace(b'\r\n', b'\n')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if '/MacOS/' in name else 0o100644) << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(temporary) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(entries)
        assert archive.getinfo(prefix + 'MacOS/DriveDrop').external_attr >> 16 & 0o111
    temporary.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.sha256').write_text(digest + '  ' + destination.name + '\n', encoding='utf-8')
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    digest = package(Path(__file__).resolve().parents[1], args.output)
    print(str(args.output) + '\nSHA256: ' + digest)

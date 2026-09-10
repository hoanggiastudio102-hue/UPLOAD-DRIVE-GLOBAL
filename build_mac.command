#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
  echo "Run this script on a Mac. Windows cannot produce a tested macOS bundle."
  exit 1
fi
python3 -m venv .build-venv-mac
build_python="$PWD/.build-venv-mac/bin/python3"
"$build_python" -m pip install -r requirements-build.txt
"$build_python" -m unittest discover -s tests -v
"$build_python" -m PyInstaller --noconfirm --clean --onedir --windowed --name DriveDrop-Boss --osx-bundle-identifier local.drivedrop.boss --distpath dist/mac --workpath .build/mac-boss --specpath .build run_boss.py
"$build_python" -m PyInstaller --noconfirm --clean --onedir --windowed --name DriveDrop-Employee --osx-bundle-identifier local.drivedrop.employee --distpath dist/mac --workpath .build/mac-employee --specpath .build run_client.py
cp README_VI.md dist/mac/README_VI.md
echo "Built dist/mac/DriveDrop-Boss.app and DriveDrop-Employee.app for this Mac's architecture."
echo "These local builds are unsigned; distributing trusted installers requires signing/notarization."

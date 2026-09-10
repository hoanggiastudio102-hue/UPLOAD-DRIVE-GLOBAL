param([string]$Python = "py")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)) {
    throw "Run this script on Windows. Use build_mac.command on macOS."
}
& $Python -m venv .build-venv
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12+ with Tcl/Tk, or provide -Python full-path-to-python.exe." }
$BuildPython = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"
& $BuildPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
& $BuildPython -c 'import tkinter; root=tkinter.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()'
if ($LASTEXITCODE -ne 0) { throw "Python Tcl/Tk is broken. Install a complete official Python with Tk before building." }
& $BuildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed; build stopped." }
& $BuildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name DriveDrop-Boss --add-data "$(Join-Path $PSScriptRoot 'drivedrop/web');drivedrop/web" --distpath dist/windows --workpath .build/windows-boss --specpath .build run_boss.py
if ($LASTEXITCODE -ne 0) { throw "Boss build failed." }
$DriveDropRuntimeCheck = Join-Path $PSScriptRoot '.build\runtime-check'
$DriveDropSmoke = Start-Process -FilePath (Join-Path $PSScriptRoot 'dist\windows\DriveDrop-Boss\DriveDrop-Boss.exe') -ArgumentList @('--data-dir', ('"' + $DriveDropRuntimeCheck + '"'), 'runtime-check') -WindowStyle Hidden -PassThru -Wait
if ($DriveDropSmoke.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $DriveDropRuntimeCheck 'runtime-check.json'))) { throw 'Packaged runtime check failed.' }
& $BuildPython -m PyInstaller --noconfirm --clean --onedir --windowed --name DriveDrop-Employee --distpath dist/windows --workpath .build/windows-employee --specpath .build run_client.py
if ($LASTEXITCODE -ne 0) { throw "Employee build failed." }
Copy-Item -LiteralPath README_VI.md -Destination dist/windows/README_VI.md -Force
Write-Host "Built dist/windows/DriveDrop-Boss and DriveDrop-Employee. Copy the whole relevant folder, including _internal."
Write-Host "Runtime state, enrollment files and OAuth JSON must NOT be added to employee bundles."

param([string]$Python = "py")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
& $Python -m unittest discover -s tests -v
exit $LASTEXITCODE

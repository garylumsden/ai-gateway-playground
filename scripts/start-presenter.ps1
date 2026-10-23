[CmdletBinding()]
param([switch]$NoBrowser, [ValidateRange(0, 65535)][int]$Port = 0)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Install the presenter dependencies in .venv using the setup instructions in README.md.'
}
$arguments = @((Join-Path $PSScriptRoot 'presenter.py'), '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
& $python @arguments
exit $LASTEXITCODE

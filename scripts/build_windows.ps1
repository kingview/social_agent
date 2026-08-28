$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

& (Join-Path $ProjectDir "scripts\install_harness.ps1")
$NodeExe = if ($env:SOCIAL_AGENT_NODE) { $env:SOCIAL_AGENT_NODE } else { (Get-Command node.exe).Source }

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  throw "Missing .venv. Install social_content_crawler plus this package first."
}

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name SocialAgent `
  --paths src `
  --add-data "$ProjectDir\harness;harness" `
  --add-binary "$NodeExe;." `
  --exclude-module PIL `
  --exclude-module numpy `
  --collect-submodules social_ops_agent `
  desktop_main.py

Write-Host "Built: $ProjectDir\dist\SocialAgent\SocialAgent.exe"

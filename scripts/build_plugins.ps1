param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$ToolsDirectory = (Resolve-Path (Join-Path $ProjectDirectory "..\tools")).Path
$Python = Join-Path $ProjectDirectory ".venv\Scripts\python.exe"
$PluginPython = $env:SOCIAL_AGENT_PLUGIN_PYTHON
if (-not (Test-Path $Python)) {
    throw "Missing $Python"
}
if (-not $PluginPython) {
    $PluginPython = $Python
}
if (-not (Test-Path $PluginPython)) {
    throw "Missing compatible plugin Python: $PluginPython"
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectDirectory "dist\plugins"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
& $Python (Join-Path $PSScriptRoot "sync_diagnostics.py") --tools-root $ToolsDirectory
if ($LASTEXITCODE -ne 0) { throw "Failed to synchronize Tool diagnostics" }

$TemporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("social-agent-plugins-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $TemporaryDirectory | Out-Null

function Build-ToolPlugin {
    param(
        [string]$SourceDirectory,
        [string]$OutputName
    )

    $WheelDirectory = Join-Path $TemporaryDirectory $OutputName
    New-Item -ItemType Directory -Force -Path $WheelDirectory | Out-Null
    & $Python -m pip wheel --no-deps --wheel-dir $WheelDirectory $SourceDirectory
    if ($LASTEXITCODE -ne 0) { throw "Failed to build $OutputName wheel" }
    $Wheel = Get-ChildItem -Path $WheelDirectory -Filter "*.whl" | Select-Object -First 1
    if (-not $Wheel) { throw "No wheel produced for $OutputName" }
    $Manifest = Join-Path $SourceDirectory "plugin\plugin.json"
    $Lock = (& $Python -m social_ops_agent.plugin_cli lock `
        --manifest $Manifest `
        --wheel $Wheel.FullName `
        --python $PluginPython `
        --output-directory $WheelDirectory | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Lock) { throw "Failed to lock $OutputName dependencies" }
    & $Python -m social_ops_agent.plugin_cli bundle `
        --manifest $Manifest `
        --wheel $Wheel.FullName `
        --lock $Lock `
        --output (Join-Path $OutputDirectory "$OutputName.socialtool")
    if ($LASTEXITCODE -ne 0) { throw "Failed to bundle $OutputName" }
}

try {
    Build-ToolPlugin (Join-Path $ToolsDirectory "social_content_crawler") "social-content"
    Build-ToolPlugin (Join-Path $ToolsDirectory "media_content_analyzer") "media-content"
    Write-Host "Built Tool plugins in $OutputDirectory"
}
finally {
    if (Test-Path $TemporaryDirectory) {
        Remove-Item -Recurse -Force $TemporaryDirectory
    }
}

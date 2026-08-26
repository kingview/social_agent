$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$NodeExe = if ($env:SOCIAL_AGENT_NODE) { $env:SOCIAL_AGENT_NODE } else { "node.exe" }
$NodeVersion = (& $NodeExe --version).Trim()
$Match = [regex]::Match($NodeVersion, '^v(?<major>\d+)\.(?<minor>\d+)\.')
if (-not $Match.Success) {
  throw "Unable to determine Node.js version from '$NodeVersion'."
}
$Major = [int]$Match.Groups['major'].Value
$Minor = [int]$Match.Groups['minor'].Value
if ($Major -lt 22 -or ($Major -eq 22 -and $Minor -lt 19)) {
  throw "DeepSeek Harness requires Node.js 22.19+ or 24+; found $NodeVersion."
}

$NpmExe = Join-Path (Split-Path -Parent (Get-Command $NodeExe).Source) "npm.cmd"
if (-not (Test-Path $NpmExe)) {
  $NpmExe = "npm.cmd"
}
$env:PATH = "$(Split-Path -Parent (Get-Command $NodeExe).Source);$env:PATH"
& $NpmExe --prefix (Join-Path $ProjectDir "harness") ci
& $NodeExe --check (Join-Path $ProjectDir "harness\node_modules\@deepseek-ai\dsh-sdk-jsonrpc-demo\lib\bin.js")
Write-Host "DeepSeek Harness dependencies are ready ($NodeVersion)."

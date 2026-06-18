param(
  [string]$Thread = "",
  [string]$Workspace = "",
  [string]$Prefill = "user_default",
  [string]$GatewayToken = "",
  [string]$Model = "session-capsules/fake-model"
)

$ErrorActionPreference = "Stop"

if (-not $Workspace) {
  $Workspace = (Get-Location).Path
}

if (-not $Thread) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Workspace)
  $hash = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant().Substring(0, 12)
  $Thread = "opencode-$hash"
}

$env:CAPSULE_WORKSPACE = $Workspace
$env:CAPSULE_THREAD = $Thread
$env:CAPSULE_PREFILL = $Prefill
if ($GatewayToken) {
  $env:CAPSULE_GATEWAY_TOKEN = $GatewayToken
} elseif (-not $env:CAPSULE_GATEWAY_TOKEN) {
  $env:CAPSULE_GATEWAY_TOKEN = "sk-capsule-local"
}

Write-Host "CAPSULE_WORKSPACE=$env:CAPSULE_WORKSPACE"
Write-Host "CAPSULE_THREAD=$env:CAPSULE_THREAD"
Write-Host "CAPSULE_PREFILL=$env:CAPSULE_PREFILL"
Write-Host "CAPSULE_GATEWAY_TOKEN=set"

opencode --model $Model

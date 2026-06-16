param(
    [string]$BaseUrl = "http://localhost:8080",
    [int]$SourceSlot = 0,
    [int]$RestoreSlot = 1,
    [string]$CapsuleFile = "demo_capsule.bin"
)

$headers = @{
    "Content-Type" = "application/json"
}

Write-Host "Checking slots at $BaseUrl/slots"
Invoke-RestMethod -Method Get -Uri "$BaseUrl/slots" | ConvertTo-Json -Depth 5

Write-Host ""
Write-Host "Example save request"
$saveBody = @{ filename = $CapsuleFile } | ConvertTo-Json
Write-Host "POST $BaseUrl/slots/$SourceSlot?action=save"
Write-Host $saveBody

Write-Host ""
Write-Host "Example restore request"
$restoreBody = @{ filename = $CapsuleFile } | ConvertTo-Json
Write-Host "POST $BaseUrl/slots/$RestoreSlot?action=restore"
Write-Host $restoreBody

Write-Host ""
Write-Host "Uncomment the commands below after verifying your server configuration."

# Invoke-RestMethod -Method Post -Uri "$BaseUrl/slots/$SourceSlot?action=save" -Headers $headers -Body $saveBody
# Invoke-RestMethod -Method Post -Uri "$BaseUrl/slots/$RestoreSlot?action=restore" -Headers $headers -Body $restoreBody


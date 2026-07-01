# deploy_manual.ps1
# Fast deploy from source (recommended for local operations)

param(
    [string]$ProjectId = "listreader",
    [string]$Region = "southamerica-east1",
    [string]$ServiceName = "leitor-ocr"
)

$ErrorActionPreference = "Stop"

Write-Host "== Manual deploy from source ==" -ForegroundColor Cyan
Write-Host "project=$ProjectId region=$Region service=$ServiceName"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud not found."
}

$null = gcloud auth print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Not authenticated. Run: gcloud auth login"
}

gcloud run deploy $ServiceName `
    --source . `
    --region $Region `
    --project $ProjectId `
    --quiet

if ($LASTEXITCODE -ne 0) {
    throw "Cloud Run deploy failed."
}

Write-Host "== Deploy completed ==" -ForegroundColor Green

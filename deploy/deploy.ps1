# deploy.ps1
# Simple Cloud Build + Cloud Run deploy

param(
    [string]$ProjectId = "listreader",
    [string]$Region = "southamerica-east1",
    [string]$ServiceName = "leitor-ocr",
    [string]$Repository = "leitor-ocr-repo",
    [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"

Write-Host "== Deploy started ==" -ForegroundColor Cyan
Write-Host "project=$ProjectId region=$Region service=$ServiceName repo=$Repository tag=$Tag"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud not found. Install Google Cloud SDK."
}

$null = gcloud auth print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Not authenticated. Run: gcloud auth login"
}

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project=$ProjectId --quiet | Out-Null

gcloud artifacts repositories describe $Repository --location=$Region --project=$ProjectId 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $Repository --location=$Region --project=$ProjectId --repository-format=docker --quiet | Out-Null
}

gcloud builds submit `
    --config=cloudbuild.yaml `
    --substitutions=_SERVICE=$ServiceName,_REGION=$Region,_REPOSITORY=$Repository,_TAG=$Tag `
    --project=$ProjectId `
    --quiet

if ($LASTEXITCODE -ne 0) {
    throw "Cloud Build failed."
}

Write-Host "== Deploy completed ==" -ForegroundColor Green
Write-Host "URL: https://$ServiceName-73372921179.$Region.run.app"

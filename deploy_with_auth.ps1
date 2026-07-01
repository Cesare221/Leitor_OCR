# deploy_with_auth.ps1
# Build image locally and deploy image to Cloud Run

param(
    [string]$ProjectId = "listreader",
    [string]$Region = "southamerica-east1",
    [string]$ServiceName = "leitor-ocr",
    [string]$Repository = "leitor-ocr-repo"
)

$ErrorActionPreference = "Stop"

$image = "$Region-docker.pkg.dev/$ProjectId/$Repository/$ServiceName:latest"

Write-Host "== Deploy image with user auth ==" -ForegroundColor Cyan
Write-Host "image=$image"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud not found."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker not found."
}

$null = gcloud auth print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Not authenticated. Run: gcloud auth login"
}

gcloud services enable run.googleapis.com artifactregistry.googleapis.com --project=$ProjectId --quiet | Out-Null
gcloud artifacts repositories describe $Repository --location=$Region --project=$ProjectId 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $Repository --location=$Region --project=$ProjectId --repository-format=docker --quiet | Out-Null
}

docker build -t $image .
if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed."
}

docker push $image
if ($LASTEXITCODE -ne 0) {
    throw "Docker push failed."
}

gcloud run deploy $ServiceName `
    --image $image `
    --region $Region `
    --project $ProjectId `
    --allow-unauthenticated `
    --quiet

if ($LASTEXITCODE -ne 0) {
    throw "Cloud Run deploy failed."
}

Write-Host "== Deploy completed ==" -ForegroundColor Green

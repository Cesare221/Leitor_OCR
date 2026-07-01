param(
    [string]$Project = "listreader",
    [string]$Region = "southamerica-east1",
    [string]$Service = "leitor-ocr"
)

$ErrorActionPreference = "Stop"

Write-Output "== Production readiness check =="
Write-Output "project=$Project region=$Region service=$Service"

$svcJson = gcloud run services describe $Service --region $Region --project $Project --format=json | ConvertFrom-Json
$url = $svcJson.status.url
$rev = $svcJson.status.latestReadyRevisionName
$envs = @{}
foreach ($env in $svcJson.spec.template.spec.containers[0].env) {
    $envs[$env.name] = $env.value
}

Write-Output ""
Write-Output "== Service =="
Write-Output "revision=$rev"
Write-Output "url=$url"

$dashboardCode = curl.exe -s -o NUL -w "%{http_code}" "$url/dashboard"
$rootCode = curl.exe -s -o NUL -w "%{http_code}" "$url/"
Write-Output "dashboard_http=$dashboardCode"
Write-Output "root_http=$rootCode"

Write-Output ""
Write-Output "== Key envs =="
$keys = @(
    "OCR_STABLE_PRODUCTION_MODE",
    "OCR_STORAGE_MODE",
    "OCR_USE_GEMINI",
    "OCR_USE_DOCUMENTAI",
    "OCR_DOCUMENTAI_PROJECT_ID",
    "OCR_DOCUMENTAI_LOCATION",
    "OCR_DOCUMENTAI_PROCESSOR_ID",
    "OCR_GEMINI_MAX_CONCURRENCY",
    "OCR_GEMINI_STABLE_MAX_CONCURRENCY",
    "OCR_GEMINI_FAST_MODEL_AVAILABLE",
    "OCR_GEMINI_WARMUP_ENABLED",
    "OCR_GEMINI_WARMUP_TIMEOUT_SECONDS",
    "OCR_GEMINI_WARMUP_TTL_SECONDS",
    "OCR_JOB_WORKERS",
    "OCR_TIMING_LOGS"
)
foreach ($k in $keys) {
    if ($envs.ContainsKey($k)) {
        Write-Output "$k=$($envs[$k])"
    } else {
        Write-Output "$k=<missing>"
    }
}

Write-Output ""
Write-Output "== Firestore jobs index =="
$idx = gcloud firestore indexes composite list --project $Project --format=json | ConvertFrom-Json
$jobsIdx = $idx | Where-Object {
    $_.queryScope -eq "COLLECTION" -and
    $_.name -match "/collectionGroups/jobs/indexes/" -and
    $_.state -eq "READY" -and
    ($_.fields | Where-Object { $_.fieldPath -eq "user_id" -and $_.order -eq "ASCENDING" }) -and
    ($_.fields | Where-Object { $_.fieldPath -eq "created_at" -and $_.order -eq "DESCENDING" })
}
if ($jobsIdx) {
    Write-Output "jobs_index_ready=true"
} else {
    Write-Output "jobs_index_ready=false"
}

Write-Output ""
Write-Output "== Summary =="
if ($dashboardCode -eq "200" -and $jobsIdx) {
    Write-Output "status=READY"
    exit 0
}

Write-Output "status=NEEDS_ATTENTION"
exit 1

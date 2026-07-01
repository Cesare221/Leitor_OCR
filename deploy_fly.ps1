# deploy_fly.ps1
# Script de deploy para Fly.io
# Pré-requisito: flyctl instalado (https://fly.io/docs/hands-on/install-flyctl/)
#
# Uso:
#   .\deploy_fly.ps1              # deploy normal
#   .\deploy_fly.ps1 -Init        # primeiro deploy (cria app + volume)

param(
    [switch]$Init
)

$APP_NAME   = "leitor-ocr"
$REGION     = "gru"
$VOLUME_SIZE = 3  # GB

# ─── Verificações ────────────────────────────────────────────────────────────

if (-not (Get-Command flyctl -ErrorAction SilentlyContinue)) {
    Write-Error "flyctl nao encontrado. Instale em: https://fly.io/docs/hands-on/install-flyctl/"
    exit 1
}

# ─── Primeiro deploy ─────────────────────────────────────────────────────────

if ($Init) {
    Write-Host "==> Criando app no Fly.io..." -ForegroundColor Cyan
    flyctl apps create $APP_NAME --machines

    Write-Host "==> Criando volume persistente ($VOLUME_SIZE GB) em $REGION..." -ForegroundColor Cyan
    flyctl volumes create leitor_ocr_data `
        --app $APP_NAME `
        --region $REGION `
        --size $VOLUME_SIZE

    Write-Host ""
    Write-Host "==> Configurando secrets necessarios..." -ForegroundColor Yellow
    Write-Host "    Voce precisara informar os seguintes secrets:"
    Write-Host ""

    $geminiKey = Read-Host "    OCR_GEMINI_API_KEY (API Key do Google AI Studio)"
    flyctl secrets set OCR_GEMINI_API_KEY="$geminiKey" --app $APP_NAME

    Write-Host ""
    Write-Host "    GOOGLE_APPLICATION_CREDENTIALS_JSON:"
    Write-Host "    Cole o caminho para o arquivo JSON da service account do GCP"
    Write-Host "    (necessario apenas para Document AI)"
    $saPath = Read-Host "    Caminho do arquivo JSON (deixe vazio para pular)"

    if ($saPath -and (Test-Path $saPath)) {
        $saContent = Get-Content $saPath -Raw
        flyctl secrets set GOOGLE_APPLICATION_CREDENTIALS_JSON="$saContent" --app $APP_NAME
        Write-Host "    Service account configurada." -ForegroundColor Green
    } else {
        Write-Host "    Pulado. Configure depois com:" -ForegroundColor Yellow
        Write-Host "    flyctl secrets set GOOGLE_APPLICATION_CREDENTIALS_JSON=<conteudo-json>"
    }

    $setupToken = Read-Host "    OCR_SETUP_TOKEN (token para criar o primeiro usuario, pode ser qualquer texto)"
    flyctl secrets set OCR_SETUP_TOKEN="$setupToken" --app $APP_NAME

    Write-Host ""
    Write-Host "==> Fazendo deploy inicial..." -ForegroundColor Cyan
    flyctl deploy --app $APP_NAME

    Write-Host ""
    Write-Host "==> Deploy concluido!" -ForegroundColor Green
    Write-Host "    Acesse: https://$APP_NAME.fly.dev"
    Write-Host "    Crie o primeiro usuario em: https://$APP_NAME.fly.dev/setup"
    exit 0
}

# ─── Deploy normal ────────────────────────────────────────────────────────────

Write-Host "==> Fazendo deploy de atualizacao..." -ForegroundColor Cyan
flyctl deploy --app $APP_NAME

Write-Host ""
Write-Host "==> Concluido!" -ForegroundColor Green
Write-Host "    App: https://$APP_NAME.fly.dev"

# Deploy no Google Cloud Run

Este guia explica como fazer deploy do Leitor OCR no Google Cloud Run.

## PrÃ©-requisitos

1. Conta no Google Cloud com faturamento habilitado
2. gcloud CLI instalado: https://cloud.google.com/sdk/docs/install
3. Projeto criado (ID: `listreader`)

## Passo 1: Configurar autenticaÃ§Ã£o

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project listreader
```

## Passo 2: Habilitar serviÃ§os necessÃ¡rios

```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    documentai.googleapis.com \
    --project=listreader
```

## Passo 3: Escolher mÃ©todo de deploy

### MÃ©todo A: Script automÃ¡tico (recomendado)

#### Windows (PowerShell):
```powershell
powershell -ExecutionPolicy Bypass -File deploy.ps1
```

#### Linux/Mac (Bash):
```bash
chmod +x deploy.sh
./deploy.sh
```

### MÃ©todo B: Manual com gcloud

```bash
# Cria repositÃ³rio de container
gcloud artifacts repositories create leitor-ocr-repo \
    --location=southamerica-east1 \
    --project=listreader \
    --repository-format=docker

# Faz build e deploy
gcloud builds submit \
    --config=cloudbuild.yaml \
    --substitutions=_SERVICE=leitor-ocr,_REGION=southamerica-east1,_REPOSITORY=leitor-ocr-repo,_TAG=v1.0.0 \
    --project=listreader
```

### MÃ©todo C: Manual passo a passo

```bash
# 1. ConstrÃ³i a imagem
gcloud builds submit --tag=gcr.io/listreader/leitor-ocr:latest

# 2. Faz deploy
gcloud run deploy leitor-ocr \
    --image=gcr.io/listreader/leitor-ocr:latest \
    --region=southamerica-east1 \
    --platform=managed \
    --allow-unauthenticated \
    --memory=2Gi \
    --set-env-vars=OCR_USE_GEMINI=true \
    --set-env-vars=OCR_USE_DOCUMENTAI=true \
    --set-env-vars=OCR_STABLE_PRODUCTION_MODE=true \
    --set-env-vars=OCR_GEMINI_MAX_CONCURRENCY=3 \
    --set-env-vars=OCR_GEMINI_SMALL_DPI=180 \
    --set-env-vars=OCR_GEMINI_MEDIUM_DPI=160 \
    --set-env-vars=OCR_GEMINI_LARGE_DPI=140 \
    --set-env-vars=OCR_GEMINI_MIN_ROWS_PER_PAGE=8 \
    --set-env-vars=OCR_GEMINI_LOCATION=southamerica-east1 \
    --set-env-vars=OCR_GEMINI_MODEL=gemini-2.5-flash \
    --set-env-vars=OCR_TIMING_LOGS=true \
    --set-env-vars=OCR_STORAGE_MODE=cloud \
    --set-env-vars=OCR_DOCUMENTAI_PROJECT_ID=listreader \
    --set-env-vars=OCR_DOCUMENTAI_LOCATION=us \
    --set-env-vars=OCR_DOCUMENTAI_PROCESSOR_ID=c50310d2f5b3f7a7
```

## Passo 4: Criar processor do Document AI

Acesse: https://console.cloud.google.com/document-ai/processors?project=listreader

Crie um processor do tipo **"Form Parser"** com nome `attendance-processor`.

## Passo 5: Testar o deploy

ApÃ³s o deploy, vocÃª receberÃ¡ uma URL como:
```
https://leitor-ocr-southamerica-east1.run.app
```

Teste com:
```bash
curl https://leitor-ocr-southamerica-east1.run.app
```

Para medir um PDF real com limite de tempo no ambiente configurado:
```bash
python benchmark_attendance.py caminho/do/arquivo.pdf --assert-max-seconds 10
```

Para validar prontidao + benchmark + relatorio em lote (aceite):
```powershell
powershell -ExecutionPolicy Bypass -Command "& .\run_acceptance_pack.ps1 -Files @('arquivo2p.pdf','arquivo6p.pdf','arquivo10p.pdf','arquivo20p.pdf') -MaxAvgSecondsPerPage 12"
```

## VariÃ¡veis de ambiente configuradas

| VariÃ¡vel | Valor | DescriÃ§Ã£o |
|----------|-------|-----------|
| `OCR_USE_GEMINI` | `true` | Habilita Gemini como processador principal |
| `OCR_USE_DOCUMENTAI` | `true` | Mantem Document AI como fallback seletivo |
| `OCR_STABLE_PRODUCTION_MODE` | `true` | Trava defaults de producao para estabilidade |
| `OCR_GEMINI_MAX_CONCURRENCY` | `3` | Limite de paginas processadas em paralelo |
| `OCR_GEMINI_SMALL_DPI` | `180` | DPI do perfil pequeno |
| `OCR_GEMINI_MEDIUM_DPI` | `160` | DPI do perfil medio |
| `OCR_GEMINI_LARGE_DPI` | `140` | DPI do perfil grande |
| `OCR_GEMINI_MIN_ROWS_PER_PAGE` | `8` | Minimo de linhas por pagina antes de acionar fallback |
| `OCR_GEMINI_LOCATION` | `southamerica-east1` | Regiao do endpoint Vertex AI Gemini |
| `OCR_GEMINI_MODEL` | `gemini-2.5-flash` | Modelo Gemini principal |
| `OCR_TIMING_LOGS` | `true` | Ativa telemetria do pipeline hibrido |
| `OCR_STORAGE_MODE` | `cloud` | Usa Cloud Storage/Firestore |
| `OCR_DOCUMENTAI_PROJECT_ID` | `listreader` | ID do projeto |
| `OCR_DOCUMENTAI_LOCATION` | `us` | Regiao real do processor Document AI em producao |
| `OCR_DOCUMENTAI_PROCESSOR_ID` | `c50310d2f5b3f7a7` | ID real do processor Document AI |
| `OCR_WEB_HOST` | `0.0.0.0` | Host do servidor web |
| `OCR_DATA_DIR` | `/tmp/data` | DiretÃ³rio temporÃ¡rio |

ObservaÃ§Ã£o:
- `OCR_GEMINI_API_KEY` pode ser usada em desenvolvimento/local.
- No Cloud Run, o pipeline tambÃ©m funciona sem API key, usando ADC da service account para chamar Vertex AI.

## ConfiguraÃ§Ã£o do Cloud Run

- **RegiÃ£o**: `southamerica-east1`
- **MemÃ³ria**: 2Gi
- **CPU**: 2000m (2 nÃºcleos)
- **Timeout**: 15 minutos
- **Concorrencia**: 20 requisicoes por container
- **Escalabilidade**: AutomÃ¡tica (0 a 10 instÃ¢ncias)

## Firestore: indice recomendado para jobs

Quando `OCR_STORAGE_MODE=cloud`, o dashboard faz consulta por `user_id` com ordenacao por `created_at`.
Crie o indice composto para evitar erro de consulta:

```bash
gcloud firestore indexes composite create \
  --collection-group=jobs \
  --field-config=field-path=user_id,order=ascending \
  --field-config=field-path=created_at,order=descending \
  --project=listreader
```

Indice versionado no projeto: [firestore.indexes.json](/C:/Users/Usuario/Desktop/kironew/leitor_OCR/firestore.indexes.json).

## Ver logs

```bash
# Logs em tempo real
gcloud logs read --project=listreader --order=asc

# Logs do Cloud Build
gcloud builds list --project=listreader

# Logs do Cloud Run
gcloud run services logs list leitor-ocr --project=listreader
```

## Atualizar o deploy

Basta executar o deploy novamente. O Cloud Build criarÃ¡ uma nova versÃ£o:

```bash
# Windows
powershell -ExecutionPolicy Bypass -File deploy.ps1

# Linux/Mac
./deploy.sh
```

## Custo estimado

- **Cloud Run**: Gratuito atÃ© 2 milhÃµes de requisiÃ§Ãµes/mÃªs
- **Document AI**: ~$1.50 por 1000 pÃ¡ginas
- **Cloud Storage**: ~$0.023 por GB/mÃªs
- **Cloud Build**: ~$0.003 por minuto de build

## SoluÃ§Ã£o de problemas

### Erro: "Permission denied"

Verifique se tem permissÃµes de `Cloud Run Admin` e `Document AI Processor User`.

### Erro: "Build failed"

Verifique se todos os serviÃ§os estÃ£o habilitados e se tem quota suficiente.

### Erro: "Processor not found"

Crie o processor no Console: https://console.cloud.google.com/document-ai/processors?project=listreader

## Suporte

- DocumentaÃ§Ã£o Cloud Run: https://cloud.google.com/run/docs
- DocumentaÃ§Ã£o Document AI: https://cloud.google.com/document-ai/docs
- PreÃ§os: https://cloud.google.com/pricing


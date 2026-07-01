# Google Cloud Document AI - Guia de Configuração

Este documento explica como configurar o Google Cloud Document AI para processar listas de presença com suporte a manuscritos.

## Por que Document AI?

O Tesseract OCR tem dificuldade com manuscritos. O Google Cloud Document AI usa modelos de ML treinados especificamente para:
- Formulários e listas estruturadas
- Texto manuscrito leve
- Tabelas e entidades
- Extração de dados estruturados

## Pré-requisitos

1. Conta no Google Cloud com faturamento habilitado
2. gcloud CLI instalado: https://cloud.google.com/sdk/docs/install
3. Projeto no Google Cloud (ID: `listreader`)

## Passo 1: Instalar dependências

```bash
pip install -r requirements.txt
```

## Passo 2: Configurar autenticação

### Opção A: Usar gcloud CLI (recomendado para desenvolvimento)

```bash
gcloud auth application-default login
```

Isso criará credenciais temporárias em:
- Windows: `%APPDATA%\gcloud\application_default_credentials.json`
- Linux/Mac: `~/.config/gcloud/application_default_credentials.json`

### Opção B: Usar Service Account (recomendado para produção)

1. Crie um Service Account no Console do GCP
2. Atribua as permissões:
   - `Document AI Processor User`
   - `Storage Object Viewer` (se usar Cloud Storage)
3. Baixe o arquivo JSON de chave
4. Configure a variável de ambiente:

```bash
# Windows (cmd)
set GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\service-account.json

# Windows (PowerShell)
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\service-account.json"

# Linux/Mac
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
```

## Passo 3: Habilitar a API do Document AI

```bash
gcloud services enable documentai.googleapis.com --project=listreader
```

## Passo 4: Criar um Processor

### Opção A: Usar o script de setup

```bash
python setup_documentai.py
```

### Opção B: Criar manualmente no Console

1. Acesse: https://console.cloud.google.com/document-ai/processors?project=listreader
2. Clique em "Create Processor"
3. Escolha "Form Parser"
4. Nome: `attendance-processor`
5. Localização: `southamerica-east1`

## Passo 5: Configurar variáveis de ambiente

Crie um arquivo `.env` (ou configure no sistema):

```bash
# Project ID
OCR_DOCUMENTAI_PROJECT_ID=listreader

# Localização
OCR_DOCUMENTAI_LOCATION=southamerica-east1

# ID do processor
OCR_DOCUMENTAI_PROCESSOR_ID=attendance-processor

# Habilitar uso do Document AI
OCR_USE_DOCUMENTAI=true
```

## Passo 6: Usar no seu código

### Modo automático (Tesseract + Document AI fallback)

```python
from documentai_extractor import process_with_documentai_fallback

result = process_with_documentai_fallback(
    file_path=Path("lista_presença.pdf"),
    output_path=Path("saida.xlsx"),
    confidence_threshold=0.85
)
```

### Modo direto (apenas Document AI)

```python
from documentai_extractor import process_attendance_list_documentai

row_count = process_attendance_list_documentai(
    file_path=Path("lista_presença.pdf"),
    output_path=Path("saida.xlsx"),
    lang="pt"
)
```

## Custo estimado

- **Form Parser**: ~$1.50 por 1000 páginas
- **Primeiros 1000 páginas/mês**: Gratuitas (como parte do nível gratuito)

Veja mais em: https://cloud.google.com/document-ai/pricing

## Solução de problemas

### Erro: "No processor found"

Verifique se:
1. A API está habilitada: `gcloud services list --enabled | grep documentai`
2. O processor existe no Console
3. A localização está correta (`southamerica-east1`)

### Erro: "Permission denied"

Verifique se:
1. Credenciais estão configuradas: `gcloud auth application-default print-access-token`
2. O Service Account tem permissão de `documentai.processorUser`

### Erro: "API not enabled"

Habilite a API:
```bash
gcloud services enable documentai.googleapis.com --project=listreader
```

## Comparação: Tesseract vs Document AI

| Característica | Tesseract | Document AI |
|----------------|-----------|-------------|
| Texto impresso | ✅ Excelente | ✅ Excelente |
| Manuscrito leve | ⚠️ Difícil | ✅ Bom |
| Tabelas | ⚠️ Configuração | ✅ Automático |
| Estrutura | ⚠️ Manual | ✅ Automático |
| Custo | Gratuito | ~$1.50/1000 páginas |
| Velocidade | Rápido | Moderado |

## Suporte

- Documentação: https://cloud.google.com/document-ai/docs
- Preços: https://cloud.google.com/document-ai/pricing
- Formulário de suporte: https://cloud.google.com/support

#!/bin/bash
# deploy.sh - Script de deploy para Google Cloud Run

set -e

echo "=========================================="
echo "Deploy para Google Cloud Run"
echo "=========================================="

# Configurações (pode ser sobrescrito via variáveis de ambiente)
PROJECT_ID="${PROJECT_ID:-listreader}"
REGION="${REGION:-southamerica-east1}"
SERVICE_NAME="${SERVICE_NAME:-leitor-ocr}"
REPOSITORY="${REPOSITORY:-leitor-ocr-repo}"
TAG="${TAG:-v1.0.0}"

echo "Project ID: $PROJECT_ID"
echo "Region: $REGION"
echo "Service: $SERVICE_NAME"
echo "Repository: $REPOSITORY"
echo "Tag: $TAG"
echo ""

# Verifica se o gcloud está instalado
if ! command -v gcloud &> /dev/null; then
    echo "❌ gcloud CLI não encontrado. Instale em: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Verifica se está autenticado
echo "✓ Verificando autenticação..."
gcloud auth print-access-token &> /dev/null || {
    echo "❌ Não autenticado. Execute: gcloud auth login"
    exit 1
}

# Verifica se está autenticado com o projeto correto
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
if [ "$CURRENT_PROJECT" != "$PROJECT_ID" ]; then
    echo "⚠️  Projeto atual: $CURRENT_PROJECT"
    echo "ℹ️  Para mudar: gcloud config set project $PROJECT_ID"
fi

# Habilita serviços necessários
echo ""
echo "✓ Habilitando serviços..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    documentai.googleapis.com \
    --project=$PROJECT_ID \
    --quiet

# Cria repositório se não existir
echo ""
echo "✓ Verificando repositório..."
if ! gcloud artifacts repositories describe $REPOSITORY --location=$REGION --project=$PROJECT_ID &> /dev/null; then
    echo "Criando repositório $REPOSITORY..."
    gcloud artifacts repositories create $REPOSITORY \
        --location=$REGION \
        --project=$PROJECT_ID \
        --repository-format=docker \
        --quiet
fi

# Constrói e faz deploy
echo ""
echo "=========================================="
echo "Construindo e fazendo deploy..."
echo "=========================================="

gcloud builds submit \
    --config=cloudbuild.yaml \
    --substitutions=_SERVICE=$SERVICE_NAME,_REGION=$REGION,_REPOSITORY=$REPOSITORY,_TAG=$TAG \
    --project=$PROJECT_ID \
    --quiet

echo ""
echo "=========================================="
echo "✓ Deploy concluído!"
echo "=========================================="
echo ""
echo "URL do serviço:"
echo "https://$SERVICE_NAME-$REGION.run.app"
echo ""
echo "Para testar:"
echo "curl https://$SERVICE_NAME-$REGION.run.app"
echo ""
echo "Para ver os logs:"
echo "gcloud logs read --project=$PROJECT_ID --order=asc"
echo ""

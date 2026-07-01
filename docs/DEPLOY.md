# Deploy no Google Cloud Run - Passo a Passo

## Pré-requisitos

1. Conta no Google Cloud (https://cloud.google.com) - tem $300 de crédito grátis por 90 dias
2. Instalar o Google Cloud CLI no seu PC: https://cloud.google.com/sdk/docs/install

---

## Passo 1: Criar projeto no Google Cloud

Acesse https://console.cloud.google.com e crie um novo projeto:
- Clique em "Selecionar projeto" > "Novo projeto"
- Nome: `leitor-ocr` (ou o que preferir)
- Clique em "Criar"

---

## Passo 2: Instalar e configurar o gcloud CLI

Após instalar o Google Cloud CLI, abra o terminal e execute:

```bash
gcloud auth login
```

Vai abrir o navegador para você fazer login com sua conta Google.

Depois configure o projeto:

```bash
gcloud config set project SEU-PROJETO-ID
```

(Substitua `SEU-PROJETO-ID` pelo ID do projeto que criou)

---

## Passo 3: Habilitar as APIs necessárias

```bash
gcloud services enable cloudbuild.googleapis.com run.googleapis.com aiplatform.googleapis.com documentai.googleapis.com
```

---

## Passo 4: Fazer o deploy

Na pasta do projeto (`leitor_OCR`), execute:

```bash
gcloud run deploy leitor-ocr --source . --region southamerica-east1 --allow-unauthenticated --memory 1Gi --timeout 300
```

Explicação:
- `--source .` = usa o Dockerfile da pasta atual
- `--region southamerica-east1` = São Paulo (menor latência)
- `--allow-unauthenticated` = permite acesso sem login Google (o app tem seu próprio login)
- `--memory 1Gi` = 1GB de RAM (OCR precisa de memória)
- `--timeout 300` = 5 minutos de timeout (PDFs grandes demoram)

O primeiro deploy demora ~5 minutos. No final vai mostrar a URL:

```
Service URL: https://leitor-ocr-xxxxx-rj.a.run.app
```

---

## Passo 5: Acessar

Abra a URL no navegador. Na primeira vez, vai pedir para criar o usuário administrador.

---

## Custos estimados

Cloud Run cobra por uso:
- **Grátis**: 2 milhões de requisições/mês + 360.000 GB-segundos
- Na prática, para uso de escritório (poucos PDFs por dia): **R$0 a R$5/mês**
- O crédito inicial de $300 cobre meses de uso

---

## Comandos úteis

```bash
# Ver logs
gcloud run services logs read leitor-ocr --region southamerica-east1

# Atualizar após mudanças no código
gcloud run deploy leitor-ocr --source . --region southamerica-east1

# Parar o serviço (para não cobrar)
gcloud run services delete leitor-ocr --region southamerica-east1
```

---

## Observações importantes

1. **Dados não persistem** entre deploys no Cloud Run. Cada vez que fizer deploy, o banco (usuários, histórico) é resetado. Para produção séria, use Cloud SQL ou Firestore (o app já tem suporte com `OCR_STORAGE_MODE=cloud`).

4. **Pipeline híbrido Gemini + Document AI**: para o caminho rápido funcionar em produção, deixe habilitadas as APIs `aiplatform.googleapis.com` e `documentai.googleapis.com`.

2. **Para uso interno simples**: o modo atual (SQLite local) funciona bem enquanto o container estiver rodando. Os dados só somem se você fizer um novo deploy.

3. **Domínio personalizado**: Pode mapear um domínio próprio (ex: `ocr.suaempresa.com.br`) nas configurações do Cloud Run no console.

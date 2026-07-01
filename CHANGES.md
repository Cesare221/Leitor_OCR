# Mudanças - Integração Google Cloud Document AI

## O que foi adicionado

### 1. Novos arquivos

| Arquivo | Descrição |
|---------|-----------|
| `documentai_extractor.py` | Módulo principal de integração com Document AI |
| `setup_documentai.py` | Script de configuração do processor |
| `test_documentai.py` | Script de teste da integração |
| `DOCUMENTAI_SETUP.md` | Guia completo de configuração |
| `.env.example` | Exemplo de variáveis de ambiente |
| `.gitignore` | Arquivo para proteger credenciais |

### 2. Arquivos modificados

| Arquivo | Mudança |
|---------|---------|
| `requirements.txt` | Adicionado `google-cloud-documentai` e dependências |
| `assinatura_lista.py` | Adicionado suporte a fallback com Document AI |
| `web_app.py` | Adicionado import e função para usar Document AI |

## Como usar

### Passo 1: Instalar dependências

```bash
pip install -r requirements.txt
```

### Passo 2: Configurar autenticação

```bash
gcloud auth application-default login
```

### Passo 3: Habilitar API

```bash
gcloud services enable documentai.googleapis.com --project=listreader
```

### Passo 4: Criar processor

Acesse: https://console.cloud.google.com/document-ai/processors?project=listreader

Ou use o script:
```bash
python setup_documentai.py
```

### Passo 5: Testar

```bash
# Copie um arquivo de teste para data/uploads/teste.pdf
python test_documentai.py
```

### Passo 6: Usar no web_app

```bash
# Com Document AI
OCR_USE_DOCUMENTAI=true python web_app.py

# Sem Document AI (apenas Tesseract)
python web_app.py
```

## Comparação de custo

| Solução | Custo | Qualidade Manuscrito |
|---------|-------|---------------------|
| Tesseract (atual) | Gratuito | ⚠️ Baixa (<85%) |
| Document AI | ~$1.50/1000 páginas | ✅ Alta (>90%) |

## Próximos passos

1. Criar o processor no Console do GCP
2. Testar com seus arquivos reais
3. Ajustar thresholds conforme necessário
4. Considerar uso de modelo customizado se necessário

## Solução de problemas

Veja `DOCUMENTAI_SETUP.md` para solução de problemas comuns.

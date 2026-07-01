# Leitor OCR - Arquitetura do Sistema

## Visão Geral

Sistema web para extração automática de dados de listas de presença em PDF, incluindo texto impresso e manuscrito, usando inteligência artificial (Google Gemini Vision).

---

## Fluxo Principal

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USUÁRIO (Browser)                           │
│                                                                     │
│  1. Acessa dashboard → 2. Upload PDF → 3. Clica "Processar Lista"  │
│                                                                     │
│  4. Aguarda processamento → 5. Baixa resultado (XLSX/CSV)          │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CLOUD RUN (web_app.py)                           │
│                                                                     │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────────────────┐  │
│  │ Recebe   │──▶│ Salva PDF    │──▶│ Escolhe processador:       │  │
│  │ Upload   │   │ temporário   │   │                            │  │
│  └──────────┘   └──────────────┘   │  OCR_USE_GEMINI=true?      │  │
│                                     │    → gemini_extractor.py   │  │
│                                     │  OCR_USE_DOCUMENTAI=true?  │  │
│                                     │    → documentai_extractor  │  │
│                                     │  Senão:                    │  │
│                                     │    → assinatura_lista.py   │  │
│                                     └─────────────┬──────────────┘  │
│                                                   │                  │
│                                                   ▼                  │
│                                     ┌────────────────────────────┐  │
│                                     │ Gera XLSX/CSV com resultado│  │
│                                     │ Registra job no banco      │  │
│                                     │ Redireciona para dashboard │  │
│                                     └────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Processadores (3 opções, em ordem de prioridade)

### 1. Gemini Vision (gemini_extractor.py) ⭐ Principal

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   PDF       │────▶│  PyMuPDF         │────▶│  Imagem PNG         │
│   (input)   │     │  (pdf_to_images) │     │  (200 DPI/página)   │
└─────────────┘     └──────────────────┘     └──────────┬──────────┘
                                                         │
                                                         ▼
                                              ┌─────────────────────┐
                                              │  Google Gemini API  │
                                              │  (gemini-2.5-flash) │
                                              │                     │
                                              │  Envia imagem +     │
                                              │  prompt estruturado │
                                              │                     │
                                              │  Retorna:           │
                                              │  - Nomes (impressos │
                                              │    e manuscritos)   │
                                              │  - Status presença  │
                                              │  - Texto manuscrito │
                                              └──────────┬──────────┘
                                                         │
                                                         ▼
                                              ┌─────────────────────┐
                                              │  Pós-processamento  │
                                              │  (postprocess_      │
                                              │   manuscrito.py)    │
                                              │                     │
                                              │  - Remove cirílicos │
                                              │  - Corrige nomes    │
                                              │  - Classifica tipo  │
                                              └──────────┬──────────┘
                                                         │
                                                         ▼
                                              ┌─────────────────────┐
                                              │  XLSX / CSV         │
                                              │  (output)           │
                                              └─────────────────────┘
```

### 2. Document AI (documentai_extractor.py) — Fallback

```
PDF → Document AI Form Parser → Extrai tabela + texto → Pós-processamento → XLSX
```
- Bom para estrutura de tabela
- Limitado para manuscritos

### 3. Tesseract OCR (assinatura_lista.py) — Fallback local

```
PDF → Imagens → Detecta grid da tabela → OCR por célula → Pós-processamento → XLSX
```
- Funciona offline
- Mais lento e menos preciso

---

## Estrutura de Arquivos

```
leitor_OCR/
├── web_app.py                  # Servidor HTTP (interface web + API)
├── gemini_extractor.py         # ⭐ Processador principal (Gemini Vision)
├── documentai_extractor.py     # Processador fallback (Google Document AI)
├── assinatura_lista.py         # Processador fallback (Tesseract local)
├── extrator_ocr.py             # Utilitários: PDF→imagem, escrita XLSX/CSV
├── postprocess_manuscrito.py   # Pós-processamento de texto manuscrito
├── gemini_ocr.py               # Módulo auxiliar Gemini (não usado atualmente)
├── Dockerfile                  # Container para Cloud Run
├── requirements.txt            # Dependências Python
├── cloud-run-config.yaml       # Configuração do serviço
├── cloudbuild.yaml             # Build automático
├── static/styles.css           # Estilos da interface web
└── data/                       # Dados locais (uploads, outputs, SQLite)
```

---

## Variáveis de Ambiente

| Variável | Valor | Descrição |
|----------|-------|-----------|
| `OCR_USE_GEMINI` | `true` | Ativa Gemini como processador principal |
| `OCR_USE_DOCUMENTAI` | `true` | Ativa Document AI como fallback |
| `OCR_GEMINI_API_KEY` | `AIza...` | Chave da API Gemini |
| `OCR_GEMINI_MODEL` | `gemini-2.5-flash` | Modelo Gemini a usar |
| `OCR_DOCUMENTAI_PROJECT_ID` | `listreader` | Projeto GCP |
| `OCR_DOCUMENTAI_LOCATION` | `us` | Região do Document AI |
| `OCR_DOCUMENTAI_PROCESSOR_ID` | `c50310...` | ID do Form Parser |
| `OCR_STORAGE_MODE` | `local` | Armazenamento local no container |

---

## Formato de Saída (XLSX)

| Coluna | Descrição |
|--------|-----------|
| Lista de Presença - Módulo | Ex: "Modulo I" |
| Curso de Formação em | Ex: "Constelação Familiar" |
| Turma | Ex: "Turma 18" |
| Data | Ex: "21/10/2022" |
| Nome Digitalizado | Nome da pessoa (impresso ou manuscrito) |
| Período | Matutino / Vespertino / Noturno |
| Assinatura (Presente/Ausente) | Status de presença |
| Tipo de Marca | nao_assinado / rubrica / nome_manuscrito / marcacao |
| Texto Manuscrito | Transcrição do que foi escrito à mão |

---

## Tecnologias

- **Backend**: Python 3.12 (servidor HTTP puro, sem framework)
- **AI/OCR**: Google Gemini 2.5 Flash (visão computacional)
- **Fallback OCR**: Google Document AI, Tesseract
- **PDF**: PyMuPDF (conversão PDF → imagem)
- **Output**: openpyxl (geração de Excel)
- **Deploy**: Google Cloud Run (container Docker)
- **Autenticação**: PBKDF2, sessões com cookie HttpOnly
- **Segurança**: CSRF, rate limiting, auditoria, LGPD

---

## Diagrama de Sequência (Processamento)

```
Browser          Cloud Run         Gemini API
  │                  │                  │
  │── POST /process ─▶│                  │
  │   (PDF upload)   │                  │
  │                  │── PDF→Imagens ──▶│
  │                  │                  │
  │                  │── Página 1 ─────▶│
  │                  │◀── Linhas 1-25 ──│
  │                  │                  │
  │                  │── Página 2 ─────▶│
  │                  │◀── Linhas 1-25 ──│
  │                  │                  │
  │                  │── Página N ─────▶│
  │                  │◀── Linhas 1-25 ──│
  │                  │                  │
  │                  │── Pós-process ──▶│
  │                  │── Gera XLSX ────▶│
  │                  │                  │
  │◀── 303 redirect ─│                  │
  │   (dashboard)    │                  │
  │                  │                  │
  │── GET /download ─▶│                  │
  │◀── XLSX file ────│                  │
```

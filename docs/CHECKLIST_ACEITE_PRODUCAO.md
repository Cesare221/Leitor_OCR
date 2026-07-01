# Checklist de Aceite - Modo Producao Estavel

## 1) Configuracao obrigatoria

- `OCR_STABLE_PRODUCTION_MODE=true`
- `OCR_USE_GEMINI=true`
- `OCR_USE_DOCUMENTAI=true`
- `OCR_STORAGE_MODE=cloud` (ou `local` para ambiente sem Firestore/GCS)
- `OCR_TIMING_LOGS=true`

## 2) Fallback Document AI validado

- Dependencia instalada: `google-cloud-documentai`
- Variaveis preenchidas:
  - `OCR_DOCUMENTAI_PROJECT_ID`
  - `OCR_DOCUMENTAI_LOCATION`
  - `OCR_DOCUMENTAI_PROCESSOR_ID`
- Observacao de regiao:
  - use a regiao em que o processor realmente existe (ex.: `us`).
  - nao presumir `southamerica-east1` para Document AI sem processor ativo nela.
- Service Account do Cloud Run com permissao de uso no Document AI
- Em log, no inicio do job:
  - `[HybridOCR] documentai_status enabled=true available=true reason=ok`

## 3) Storage cloud sem falha de indice

- Aplicar indice Firestore:
  - arquivo: `deploy/firestore.indexes.json`
  - comando:
    - `gcloud firestore indexes composite create --collection-group=jobs --field-config=field-path=user_id,order=ascending --field-config=field-path=created_at,order=descending --project=listreader`
- Confirmar que `/dashboard` e `/jobs-feed` carregam sem erro 5xx.

## 4) Teste de regressao funcional

- Processar 1 PDF curto (2-4 paginas)
- Processar 1 PDF medio (6-10 paginas)
- Processar 1 PDF longo (20 paginas)
- Validar:
  - arquivo final gera download
  - colunas XLSX continuam no schema atual
  - sem presenca falsa por traco `--` impresso

## 5) Teste de tempo e estabilidade

- Executar benchmark com `OCR_TIMING_LOGS=true`
- Opcao rapida em lote:
  - `powershell -ExecutionPolicy Bypass -File .\run_benchmark_suite.ps1 -Files arquivo2p.pdf,arquivo6p.pdf,arquivo10p.pdf,arquivo20p.pdf`
- Confirmar logs com:
  - `page_count`
  - `profile_name`
  - `render_ms`
  - `gemini_ms_per_page`
  - `fallback_ms_per_page`
  - `total_ms`
- Meta operacional:
  - crescimento aproximadamente linear por pagina
  - sem timeout em lote continuo

## 6) Go-live

- Deploy com env estavel
- Rodar 3 uploads consecutivos no mesmo login/sessao
- Verificar jobs em paralelo concluindo sem travar dashboard
- Registrar baseline de tempo para acompanhamento semanal

## 7) Pacote de aceite automatizado (recomendado)

- Executar em 1 comando:
  - `powershell -ExecutionPolicy Bypass -Command "& .\run_acceptance_pack.ps1 -Files @('arquivo2p.pdf','arquivo6p.pdf','arquivo10p.pdf','arquivo20p.pdf') -MaxAvgSecondsPerPage 12"`
- Artefatos gerados automaticamente:
  - `acceptance_readiness_*.txt`
  - `benchmark_suite_*.csv`
  - `benchmark_report_*.html`
  - `acceptance_summary_*.txt` com decisao `GO/NO-GO`

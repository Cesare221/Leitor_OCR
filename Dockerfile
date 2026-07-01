FROM python:3.12-slim

# Instala dependências do sistema
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-por \
        poppler-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . .

# Cria diretórios necessários
# /data será montado como volume no Fly.io; /tmp/data serve para ambiente local
RUN mkdir -p /tmp/data/uploads /tmp/data/outputs /data/uploads /data/outputs

# Copia e prepara o entrypoint
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Variáveis de ambiente padrão (sobrescritas pelo fly.toml em produção)
ENV PORT=8080
ENV OCR_WEB_HOST=0.0.0.0
ENV OCR_DATA_DIR=/data
ENV OCR_USE_GEMINI=true
ENV OCR_USE_DOCUMENTAI=true
ENV OCR_STORAGE_MODE=local
ENV OCR_DOCUMENTAI_PROJECT_ID=listreader
ENV OCR_DOCUMENTAI_LOCATION=us
ENV OCR_DOCUMENTAI_PROCESSOR_ID=c50310d2f5b3f7a7
ENV OCR_GEMINI_MAX_CONCURRENCY=3
ENV OCR_GEMINI_SMALL_DPI=180
ENV OCR_GEMINI_MEDIUM_DPI=160
ENV OCR_GEMINI_LARGE_DPI=140
ENV OCR_GEMINI_MIN_ROWS_PER_PAGE=8
ENV OCR_GEMINI_LOCATION=southamerica-east1
ENV OCR_GEMINI_MODEL=gemini-2.5-flash
ENV OCR_TIMING_LOGS=true

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]

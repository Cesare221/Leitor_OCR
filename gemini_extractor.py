"""
gemini_extractor.py - Extrator de lista de presenca usando Gemini Vision via REST API.

Usa a API REST do Gemini para enviar imagens de paginas e extrair dados
da tabela de presenca incluindo texto manuscrito.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import google.auth
import google.auth.transport.requests
import requests
from requests.adapters import HTTPAdapter

from extrator_ocr import ExtractedRow, pdf_to_images, write_output


PROJECT_ID = os.environ.get("OCR_DOCUMENTAI_PROJECT_ID", "listreader")
LOCATION = os.environ.get("OCR_GEMINI_LOCATION", "southamerica-east1")
MODEL = os.environ.get("OCR_GEMINI_MODEL", "gemini-2.5-flash")
FAST_MODEL = os.environ.get("OCR_GEMINI_FAST_MODEL", "gemini-2.0-flash-lite")
STRONG_MODEL = os.environ.get("OCR_GEMINI_STRONG_MODEL", MODEL)
API_KEY = os.environ.get("OCR_GEMINI_API_KEY", "")
MAX_OUTPUT_TOKENS_SINGLE = int(os.environ.get("OCR_GEMINI_MAX_OUTPUT_TOKENS_SINGLE", "4800"))
MAX_OUTPUT_TOKENS_BATCH = int(os.environ.get("OCR_GEMINI_MAX_OUTPUT_TOKENS_BATCH", "8192"))
TIMEOUT_SINGLE_SECONDS = int(os.environ.get("OCR_GEMINI_TIMEOUT_SINGLE_SECONDS", "70"))
TIMEOUT_BATCH_SECONDS = int(os.environ.get("OCR_GEMINI_TIMEOUT_BATCH_SECONDS", "120"))
RETRIES_SINGLE = int(os.environ.get("OCR_GEMINI_RETRIES_SINGLE", "2"))
RETRIES_BATCH = int(os.environ.get("OCR_GEMINI_RETRIES_BATCH", "2"))

_AUTH_LOCK = threading.Lock()
_FAST_MODEL_LOCK = threading.Lock()
_CREDENTIALS = None
_AUTH_REQUEST = google.auth.transport.requests.Request()
_HTTP = requests.Session()
_HTTP.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=16))
_FAST_MODEL_AVAILABLE: bool | None = None


def _post_with_retry(
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    request_timeout: int,
    max_retries: int,
) -> requests.Response:
    last_network_error: Exception | None = None
    response: requests.Response | None = None

    for attempt in range(max_retries + 1):
        try:
            response = _HTTP.post(url, json=payload, headers=headers, timeout=request_timeout)
        except requests.exceptions.RequestException as exc:
            last_network_error = exc
            if attempt < max_retries:
                time.sleep(min(4, 1 + attempt))
                continue
            raise RuntimeError(f"Gemini network error after retries: {exc}") from exc

        if response.status_code != 429:
            return response
        if attempt < max_retries:
            time.sleep(2 * (attempt + 1))

    if response is not None:
        return response
    raise RuntimeError(f"Gemini request failed: {last_network_error}")


def _configured_fast_model_availability() -> bool | None:
    raw = os.environ.get("OCR_GEMINI_FAST_MODEL_AVAILABLE", "auto").strip().lower()
    if raw in {"1", "true", "yes", "sim", "on"}:
        return True
    if raw in {"0", "false", "no", "nao", "off"}:
        return False
    return None


def _probe_fast_model_availability(timeout_seconds: int) -> bool:
    """Faz um ping leve no modelo rapido para decidir fallback antes da carga principal."""
    if FAST_MODEL == STRONG_MODEL:
        return True

    url, headers = _build_model_url(FAST_MODEL)
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "ok"}],
        }],
        "generationConfig": {
            "maxOutputTokens": 8,
            "temperature": 0,
        },
    }

    response = _HTTP.post(url, json=payload, headers=headers, timeout=max(5, timeout_seconds))
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    # Mantem fast como disponivel em erros transitivos (429/5xx) para nao bloquear
    # um caminho potencialmente mais rapido.
    return True


def warmup_gemini_runtime(timeout_seconds: int = 8) -> dict[str, object]:
    """
    Pre-aquece autenticacao e disponibilidade do modelo rapido uma vez por processo.
    Reduz latencia de primeira pagina e evita tempestade de fallback 404 em paralelo.
    """
    started = time.perf_counter()
    token_ready = False
    fast_model_available: bool | None = None
    warmup_error = ""

    try:
        if API_KEY:
            token_ready = True
        else:
            _get_access_token()
            token_ready = True
    except Exception as exc:
        warmup_error = f"token:{exc}"

    global _FAST_MODEL_AVAILABLE
    with _FAST_MODEL_LOCK:
        if _FAST_MODEL_AVAILABLE is None:
            forced = _configured_fast_model_availability()
            if forced is not None:
                _FAST_MODEL_AVAILABLE = forced
            else:
                try:
                    _FAST_MODEL_AVAILABLE = _probe_fast_model_availability(timeout_seconds)
                except Exception as exc:
                    if not warmup_error:
                        warmup_error = f"probe:{exc}"
                    # Em caso de erro de probe, evita degradar para strong sem evidencias.
                    _FAST_MODEL_AVAILABLE = True
        fast_model_available = _FAST_MODEL_AVAILABLE

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "token_ready": token_ready,
        "fast_model_available": fast_model_available,
        "elapsed_ms": elapsed_ms,
        "error": warmup_error,
    }


def _build_model_url(model_name: str) -> tuple[str, dict[str, str]]:
    if API_KEY:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model_name}:generateContent?key={API_KEY}"
        )
        headers = {"Content-Type": "application/json"}
    else:
        access_token = _get_access_token()
        url = (
            "https://aiplatform.googleapis.com/v1/"
            f"projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/{model_name}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
    return url, headers


def _get_access_token() -> str:
    """Obtem access token via Application Default Credentials."""
    global _CREDENTIALS
    with _AUTH_LOCK:
        if _CREDENTIALS is None:
            _CREDENTIALS, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

        needs_refresh = (
            not getattr(_CREDENTIALS, "token", None)
            or not getattr(_CREDENTIALS, "valid", False)
            or getattr(_CREDENTIALS, "expired", False)
        )
        expiry = getattr(_CREDENTIALS, "expiry", None)
        if expiry is not None:
            now = datetime.now(timezone.utc)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry - now <= timedelta(seconds=90):
                needs_refresh = True

        if needs_refresh:
            _CREDENTIALS.refresh(_AUTH_REQUEST)
        return _CREDENTIALS.token


def _call_gemini(
    image_bytes: bytes,
    prompt: str,
    mime_type: str = "image/png",
    model_name: str | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
) -> tuple[str, str]:
    """Chama a API do Gemini com uma imagem e prompt."""
    selected_model = (model_name or MODEL).strip() or MODEL
    global _FAST_MODEL_AVAILABLE
    if selected_model == FAST_MODEL:
        # Evita tempestade de requests 404 em paralelo quando o modelo rapido
        # nao esta disponivel na conta/regiao.
        with _FAST_MODEL_LOCK:
            if _FAST_MODEL_AVAILABLE is False:
                selected_model = STRONG_MODEL
    request_timeout = max(10, int(timeout_seconds or TIMEOUT_SINGLE_SECONDS))
    max_retries = max(0, int(retries if retries is not None else RETRIES_SINGLE))
    url, headers = _build_model_url(selected_model)

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": image_b64,
                    }
                },
                {"text": prompt},
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS_SINGLE,
            "temperature": 0,
        },
    }

    response = _post_with_retry(url, payload, headers, request_timeout, max_retries)

    if response.status_code != 200:
        if response.status_code == 404 and selected_model != STRONG_MODEL:
            if selected_model == FAST_MODEL:
                with _FAST_MODEL_LOCK:
                    _FAST_MODEL_AVAILABLE = False
            # Fallback automatico para modelo forte quando o modelo rapido
            # nao estiver disponivel na conta/regiao.
            return _call_gemini(
                image_bytes,
                prompt,
                mime_type=mime_type,
                model_name=STRONG_MODEL,
                timeout_seconds=timeout_seconds,
                retries=retries,
            )
        raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")

    if selected_model == FAST_MODEL:
        with _FAST_MODEL_LOCK:
            _FAST_MODEL_AVAILABLE = True

    data = response.json()

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:500]}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_parts = [p["text"] for p in parts if "text" in p]
    return "\n".join(text_parts), selected_model


PROMPT = """Voce e um sistema especializado em extrair dados de listas de presenca de cursos.

Analise esta imagem de UMA PAGINA de uma lista de presenca.

ESTRUTURA:
- Cabecalho: modulo, nome do curso, turma, DATA (dd/mm/aaaa)
- Tabela: Numero | Nome | Matutino | Vespertino | Noturno

REGRAS DE PRESENCA (MUITO IMPORTANTE):
- "Presente" = ha uma marca INEQUIVOCA de caneta: assinatura, rubrica, nome escrito, "Sim", "OK", risco de caneta
- "Ausente" = celula VAZIA, ou tem apenas tracos de impressao "--", ou tem apenas a borda da tabela
- TRACOS "--" impressos na celula = AUSENTE (sao o padrao da tabela para celula vazia)
- Se a celula parece vazia mas tem uma linha horizontal fina = AUSENTE (e a borda da tabela)
- Apenas marcas CLARAS de caneta/caneta = Presente

TIPOS DE MARCA (classifique diretamente):
- "nao_assinado" = celula ausente/vazia
- "marcacao" = "Sim", "OK", "X", "V", check mark, ou qualquer marcacao simples
- "rubrica" = assinatura cursiva, rabisco, iniciais, ou texto curto ilegivel (1-5 chars)
- "nome_manuscrito" = nome ou sobrenome escrito de forma legivel (6+ chars)

FORMATO DE RESPOSTA (COMPACTO, OBRIGATORIO):
H|modulo|curso|turma|data
L|nome|mat_status|mat_tipo|mat_texto|vesp_status|vesp_tipo|vesp_texto|not_status|not_tipo|not_texto

CODIGOS:
- status: P=Presente, A=Ausente
- tipo: N=nao_assinado, M=marcacao, R=rubrica, H=nome_manuscrito

EXEMPLO:
H|Modulo I|Constelacao Familiar|Turma 18|21/10/2022
L|Adriano Matias Quiste|A|N||A|N||A|N|
L|Alessandra Gomes Ribeiro de Carvalho|A|N||P|R|A|P|R|A
L|Damiana Rodrigues Pacheco de Macedo|P|H|Damiana|P|H|Damiana|P|M|Sim
L|Welbert Eduardo de Jesus|P|H|Welbert Jesus|P|H|Welbert Jesus|P|M|Sim

REGRAS CRITICAS:
- Cada linha fisica da tabela = UMA linha LINHA no output (nao divida nomes)
- Inclua TODAS as linhas, mesmo com nome manuscrito
- Data: dd/mm/aaaa exatamente como no cabecalho
- Tracos "--" impressos = Ausente/nao_assinado
- Se a celula tem apenas a linha horizontal da tabela = Ausente"""


BATCH_PROMPT = """Voce e um sistema especializado em extrair dados de listas de presenca de cursos.

Analise estas imagens consecutivas de listas de presenca. Cada imagem = uma pagina do documento.
CADA PAGINA pode ter uma DATA DIFERENTE no cabecalho - leia a data de CADA pagina individualmente.

ESTRUTURA:
- Cabecalho: modulo, nome do curso, turma, DATA (dd/mm/aaaa)
- Tabela: Numero | Nome | Matutino | Vespertino | Noturno

REGRAS DE PRESENCA (MUITO IMPORTANTE):
- "Presente" = ha uma marca INEQUIVOCA de caneta: assinatura, rubrica, nome escrito, "Sim", "OK", risco de caneta
- "Ausente" = celula VAZIA, ou tem apenas tracos de impressao "--", ou tem apenas a borda da tabela
- TRACOS "--" impressos na celula = AUSENTE (sao o padrao da tabela para celula vazia)
- Se a celula parece vazia mas tem uma linha horizontal fina = AUSENTE (e a borda da tabela)
- Apenas marcas CLARAS de caneta = Presente

TIPOS DE MARCA:
- "nao_assinado" = celula ausente/vazia
- "marcacao" = "Sim", "OK", "X", "V", check mark, ou qualquer marcacao simples
- "rubrica" = assinatura cursiva, rabisco, iniciais, ou texto curto ilegivel (1-5 chars)
- "nome_manuscrito" = nome ou sobrenome escrito de forma legivel (6+ chars)

FORMATO DE RESPOSTA (COMPACTO, OBRIGATORIO):
H|pagina|modulo|curso|turma|data
L|pagina|nome|mat_status|mat_tipo|mat_texto|vesp_status|vesp_tipo|vesp_texto|not_status|not_tipo|not_texto

CODIGOS:
- status: P=Presente, A=Ausente
- tipo: N=nao_assinado, M=marcacao, R=rubrica, H=nome_manuscrito

EXEMPLO (pagina 1):
H|1|Modulo I|Constelacao Familiar|Turma 18|21/10/2022
L|1|Adriano Matias Quiste|A|N||A|N||A|N|
L|1|Alessandra Gomes Ribeiro|A|N||P|R|A|P|R|A
L|1|Damiana Rodrigues Pacheco|P|H|Damiana|P|H|Damiana|P|M|Sim

REGRAS CRITICAS:
- Use pagina 1 para a primeira imagem, pagina 2 para a segunda, etc
- Leia a DATA do cabecalho de CADA pagina (podem ser dias diferentes)
- Cada linha fisica da tabela = UMA pessoa (nao divida nomes)
- Inclua TODAS as linhas visiveis em cada pagina
- Tracos "--" impressos = Ausente/nao_assinado
- Nao invente nomes nem remova linhas"""


def parse_gemini_response(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Converte a resposta textual do Gemini em header + linhas logicas."""
    header = {"modulo": "", "curso": "", "turma": "", "data": ""}
    rows: list[dict[str, str]] = []

    def decode_status(value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned in {"p", "presente"}:
            return "Presente"
        if cleaned in {"a", "ausente"}:
            return "Ausente"
        return value.strip()

    def decode_tipo(value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned in {"n", "nao_assinado"}:
            return "nao_assinado"
        if cleaned in {"m", "marcacao"}:
            return "marcacao"
        if cleaned in {"r", "rubrica"}:
            return "rubrica"
        if cleaned in {"h", "nome_manuscrito"}:
            return "nome_manuscrito"
        return value.strip()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split("|")

        if len(parts) >= 2 and parts[0].upper() in {"HEADER", "H"}:
            if len(parts) >= 5:
                header["modulo"] = parts[1].strip()
                header["curso"] = parts[2].strip()
                header["turma"] = parts[3].strip()
                header["data"] = parts[4].strip()
        elif parts[0].upper() in {"LINHA", "L"}:
            # Novo formato: LINHA|nome|mat_status|mat_tipo|mat_texto|vesp_status|vesp_tipo|vesp_texto|not_status|not_tipo|not_texto
            if len(parts) >= 11:
                rows.append({
                    "nome": parts[1].strip(),
                    "matutino_status": decode_status(parts[2]),
                    "matutino_tipo": decode_tipo(parts[3]),
                    "matutino_texto": parts[4].strip(),
                    "vespertino_status": decode_status(parts[5]),
                    "vespertino_tipo": decode_tipo(parts[6]),
                    "vespertino_texto": parts[7].strip(),
                    "noturno_status": decode_status(parts[8]),
                    "noturno_tipo": decode_tipo(parts[9]),
                    "noturno_texto": parts[10].strip() if len(parts) > 10 else "",
                })
            # Formato antigo: LINHA|nome|mat_status|mat_texto|vesp_status|vesp_texto|not_status|not_texto
            elif len(parts) >= 8:
                rows.append({
                    "nome": parts[1].strip(),
                    "matutino_status": decode_status(parts[2]),
                    "matutino_tipo": "",
                    "matutino_texto": parts[3].strip(),
                    "vespertino_status": decode_status(parts[4]),
                    "vespertino_tipo": "",
                    "vespertino_texto": parts[5].strip(),
                    "noturno_status": decode_status(parts[6]),
                    "noturno_tipo": "",
                    "noturno_texto": parts[7].strip() if len(parts) > 7 else "",
                })

    return header, rows


def process_page_with_gemini(
    image_path: Path,
    lang: str = "pt",
    model_name: str | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
) -> dict[str, object]:
    """Processa uma pagina/imagem unica com Gemini e retorna estrutura intermediaria."""
    del lang  # reservado para futuras variacoes de prompt
    image_bytes = image_path.read_bytes()
    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    text, model_used = _call_gemini(
        image_bytes,
        PROMPT,
        mime_type=mime_type,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        retries=retries,
    )
    header, rows = parse_gemini_response(text)
    return {
        "header": header,
        "rows": rows,
        "model_used": model_used,
    }


def process_pages_with_gemini(
    image_paths: list[Path],
    lang: str = "pt",
    model_name: str | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
) -> list[dict[str, object]]:
    """Processa um pequeno lote de paginas e devolve resultados por pagina."""
    del lang
    if not image_paths:
        return []
    if len(image_paths) == 1:
        return [process_page_with_gemini(image_paths[0], lang="pt", model_name=model_name, timeout_seconds=timeout_seconds, retries=retries)]

    parts: list[dict[str, object]] = []
    for image_path in image_paths:
        image_bytes = image_path.read_bytes()
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })
    parts.append({"text": BATCH_PROMPT})

    selected_model = (model_name or MODEL).strip() or MODEL
    global _FAST_MODEL_AVAILABLE
    if selected_model == FAST_MODEL:
        with _FAST_MODEL_LOCK:
            if _FAST_MODEL_AVAILABLE is False:
                selected_model = STRONG_MODEL
    request_timeout = max(10, int(timeout_seconds or TIMEOUT_BATCH_SECONDS))
    max_retries = max(0, int(retries if retries is not None else RETRIES_BATCH))
    url, headers = _build_model_url(selected_model)

    payload = {
        "contents": [{
            "role": "user",
            "parts": parts,
        }],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS_BATCH,
            "temperature": 0,
        },
    }

    response = _post_with_retry(url, payload, headers, request_timeout, max_retries)

    if response.status_code != 200:
        if response.status_code == 404 and selected_model != STRONG_MODEL:
            if selected_model == FAST_MODEL:
                with _FAST_MODEL_LOCK:
                    _FAST_MODEL_AVAILABLE = False
            return process_pages_with_gemini(
                image_paths,
                lang=lang,
                model_name=STRONG_MODEL,
                timeout_seconds=timeout_seconds,
                retries=retries,
            )
        raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")
    if selected_model == FAST_MODEL:
        with _FAST_MODEL_LOCK:
            _FAST_MODEL_AVAILABLE = True

    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:500]}")

    text_parts = [
        part["text"]
        for part in candidates[0].get("content", {}).get("parts", [])
        if "text" in part
    ]
    text = "\n".join(text_parts)

    headers_by_page = {
        index: {"modulo": "", "curso": "", "turma": "", "data": ""}
        for index in range(1, len(image_paths) + 1)
    }
    rows_by_page = {index: [] for index in range(1, len(image_paths) + 1)}

    def decode_status(value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned in {"p", "presente"}:
            return "Presente"
        if cleaned in {"a", "ausente"}:
            return "Ausente"
        return value.strip()

    def decode_tipo(value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned in {"n", "nao_assinado"}:
            return "nao_assinado"
        if cleaned in {"m", "marcacao"}:
            return "marcacao"
        if cleaned in {"r", "rubrica"}:
            return "rubrica"
        if cleaned in {"h", "nome_manuscrito"}:
            return "nome_manuscrito"
        return value.strip()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 6 and parts[0].upper() in {"HEADER", "H"}:
            try:
                page_number = int(parts[1].strip())
            except ValueError:
                continue
            if page_number not in headers_by_page:
                continue
            headers_by_page[page_number] = {
                "modulo": parts[2].strip(),
                "curso": parts[3].strip(),
                "turma": parts[4].strip(),
                "data": parts[5].strip(),
            }
        elif parts[0].upper() in {"LINHA", "L"}:
            try:
                page_number = int(parts[1].strip())
            except ValueError:
                continue
            if page_number not in rows_by_page:
                continue
            # Novo formato: LINHA|pagina|nome|mat_status|mat_tipo|mat_texto|vesp_status|vesp_tipo|vesp_texto|not_status|not_tipo|not_texto
            if len(parts) >= 12:
                rows_by_page[page_number].append({
                    "nome": parts[2].strip(),
                    "matutino_status": decode_status(parts[3]),
                    "matutino_tipo": decode_tipo(parts[4]),
                    "matutino_texto": parts[5].strip(),
                    "vespertino_status": decode_status(parts[6]),
                    "vespertino_tipo": decode_tipo(parts[7]),
                    "vespertino_texto": parts[8].strip(),
                    "noturno_status": decode_status(parts[9]),
                    "noturno_tipo": decode_tipo(parts[10]),
                    "noturno_texto": parts[11].strip() if len(parts) > 11 else "",
                })
            # Formato antigo: LINHA|pagina|nome|mat_status|mat_texto|vesp_status|vesp_texto|not_status|not_texto
            elif len(parts) >= 9:
                rows_by_page[page_number].append({
                    "nome": parts[2].strip(),
                    "matutino_status": parts[3].strip(),
                    "matutino_tipo": "",
                    "matutino_texto": parts[4].strip(),
                    "vespertino_status": parts[5].strip(),
                    "vespertino_tipo": "",
                    "vespertino_texto": parts[6].strip(),
                    "noturno_status": parts[7].strip(),
                    "noturno_tipo": "",
                    "noturno_texto": parts[8].strip(),
                })

    return [
        {"header": headers_by_page[index], "rows": rows_by_page[index], "model_used": selected_model}
        for index in range(1, len(image_paths) + 1)
    ]


def _process_page(image_bytes: bytes) -> list[dict]:
    """Envia uma pagina para o Gemini e extrai as linhas da tabela."""
    text, _model_used = _call_gemini(image_bytes, PROMPT)
    header, rows = parse_gemini_response(text)

    for row in rows:
        row.update(header)

    return rows


def process_attendance_list_gemini(
    file_path: Path,
    output_path: Path,
    lang: str = "pt",
) -> int:
    """
    Processa lista de presenca usando Gemini Vision via REST API.
    """
    print(f"[Gemini] Processing: {file_path.name}")

    with tempfile.TemporaryDirectory(prefix="gemini_ocr_") as temp:
        work_dir = Path(temp)

        if file_path.suffix.lower() == ".pdf":
            image_paths = pdf_to_images(file_path, work_dir, dpi=200)
        else:
            image_paths = [file_path]

        print(f"[Gemini] {len(image_paths)} pages to process")

        all_extracted: list[ExtractedRow] = []
        row_counter = 0
        last_header = {"modulo": "", "curso": "", "turma": "", "data": ""}

        for page_idx, img_path in enumerate(image_paths, start=1):
            print(f"[Gemini] Processing page {page_idx}/{len(image_paths)}...")

            image_bytes = img_path.read_bytes()

            try:
                page_rows = _process_page(image_bytes)
                print(f"[Gemini] Page {page_idx}: {len(page_rows)} rows extracted")
            except Exception as e:
                print(f"[Gemini] ERROR on page {page_idx}: {e}")
                traceback.print_exc()
                raise

            for row_data in page_rows:
                nome = row_data.get("nome", "").strip()
                if not nome:
                    continue

                if row_data.get("curso"):
                    last_header = {
                        "modulo": row_data.get("modulo") or last_header["modulo"],
                        "curso": row_data.get("curso") or last_header["curso"],
                        "turma": row_data.get("turma") or last_header["turma"],
                        "data": row_data.get("data") or last_header["data"],
                    }

                modulo = row_data.get("modulo") or last_header["modulo"]
                curso = row_data.get("curso") or last_header["curso"]
                turma = row_data.get("turma") or last_header["turma"]
                data = row_data.get("data") or last_header["data"]

                for periodo, status_key, texto_key in [
                    ("Matutino", "matutino_status", "matutino_texto"),
                    ("Vespertino", "vespertino_status", "vespertino_texto"),
                    ("Noturno", "noturno_status", "noturno_texto"),
                ]:
                    status = row_data.get(status_key, "Ausente")
                    texto = row_data.get(texto_key, "")

                    if status.lower() in ("presente", "sim", "yes", "p"):
                        presenca = "Presente"
                    else:
                        presenca = "Ausente"
                        texto = ""

                    if presenca == "Ausente":
                        tipo = "nao_assinado"
                    elif texto.lower() in ("sim", "ok", "x", "ok! sim", "ok sim"):
                        tipo = "marcacao"
                        texto = "Sim"
                    elif len(texto) > 8:
                        tipo = "nome_manuscrito"
                    elif texto:
                        tipo = "rubrica"
                    else:
                        tipo = "rubrica"

                    row_counter += 1
                    all_extracted.append(ExtractedRow(
                        source=str(file_path),
                        page=page_idx,
                        row_number=row_counter,
                        columns=[
                            modulo, curso, turma, data,
                            nome, periodo, presenca, tipo, texto,
                        ],
                    ))

    if not all_extracted:
        raise RuntimeError("Gemini nao retornou dados utilizaveis.")

    try:
        from postprocess_manuscrito import postprocess_extracted_rows
        all_extracted = postprocess_extracted_rows(all_extracted)
    except Exception as e:
        print(f"[Gemini] Postprocess skipped: {e}")

    headers = [
        "Lista de Presenca - Modulo",
        "Curso de Formacao em",
        "Turma",
        "Data",
        "Nome Digitalizado",
        "Periodo",
        "Assinatura (Presente/Ausente)",
        "Tipo de Marca",
    ]

    write_output(all_extracted, output_path, headers)
    print(f"[Gemini] Done: {len(all_extracted)} rows written to {output_path}")
    return len(all_extracted)

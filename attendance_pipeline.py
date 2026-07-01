from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from extrator_ocr import ExtractedRow, count_pdf_pages, pdf_to_images, render_pdf_page, write_output


@dataclass(frozen=True)
class ProcessingProfile:
    name: str
    dpi: int
    image_format: str
    jpeg_quality: int
    max_concurrency: int
    min_rows_per_page: int
    fallback_mode: str


@dataclass
class AttendancePageResult:
    page_number: int
    rows: list[dict[str, Any]]
    header: dict[str, str]
    processor_used: str
    timings_ms: dict[str, int] = field(default_factory=dict)


_HEADER_KEYS = ("modulo", "curso", "turma", "data")
_DOCAI_FALLBACK_AVAILABLE: bool | None = None
_DOCAI_FALLBACK_REASON: str = ""
_GEMINI_WARMUP_CACHE: dict[str, Any] | None = None
_GEMINI_WARMUP_CACHE_TS: float = 0.0


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _stable_production_mode_enabled() -> bool:
    return _env_flag("OCR_STABLE_PRODUCTION_MODE", False)


def _refine_prev_page_max_rows() -> int:
    return max(1, _env_int("OCR_GEMINI_REFINE_PREV_MAX_ROWS", 21))


def _timing_logs_enabled() -> bool:
    return _env_flag("OCR_TIMING_LOGS", True)


def _overflow_legacy_fallback_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_OVERFLOW_LEGACY_FALLBACK", False)


def _presence_guard_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_VISUAL_PRESENCE_GUARD", False)


def _absence_guard_enabled() -> bool:
    if _stable_production_mode_enabled():
        return True
    return _env_flag("OCR_ENABLE_VISUAL_ABSENCE_GUARD", True)


def _legacy_guard_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_LEGACY_SIGNATURE_GUARD", False)


def _high_quality_retry_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_HQ_RETRY", False)


def _refine_previous_page_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_PREV_PAGE_REFINE", False)


def _fast_model_first_pass_enabled() -> bool:
    if _stable_production_mode_enabled():
        return True
    return _env_flag("OCR_ENABLE_FAST_MODEL_FIRST_PASS", True)


def _fast_model_name() -> str:
    return os.environ.get("OCR_GEMINI_FAST_MODEL", "gemini-2.0-flash-lite").strip() or "gemini-2.0-flash-lite"


def _strong_model_name() -> str:
    return os.environ.get("OCR_GEMINI_STRONG_MODEL", os.environ.get("OCR_GEMINI_MODEL", "gemini-2.5-flash")).strip() or "gemini-2.5-flash"


def _fast_model_timeout_seconds() -> int:
    if _stable_production_mode_enabled():
        return 35
    return max(20, _env_int("OCR_GEMINI_FAST_TIMEOUT_SECONDS", 45))


def _strong_model_timeout_seconds() -> int:
    if _stable_production_mode_enabled():
        return 50
    return max(30, _env_int("OCR_GEMINI_STRONG_TIMEOUT_SECONDS", 90))


def _fast_model_retries() -> int:
    if _stable_production_mode_enabled():
        return 1
    return max(0, _env_int("OCR_GEMINI_FAST_RETRIES", 1))


def _strong_model_retries() -> int:
    if _stable_production_mode_enabled():
        return 1
    return max(0, _env_int("OCR_GEMINI_STRONG_RETRIES", 2))


def _gemini_warmup_enabled() -> bool:
    if _stable_production_mode_enabled():
        return True
    return _env_flag("OCR_GEMINI_WARMUP_ENABLED", True)


def _gemini_warmup_timeout_seconds() -> int:
    if _stable_production_mode_enabled():
        return 8
    return max(5, _env_int("OCR_GEMINI_WARMUP_TIMEOUT_SECONDS", 8))


def _gemini_warmup_ttl_seconds() -> int:
    if _stable_production_mode_enabled():
        return max(60, _env_int("OCR_GEMINI_WARMUP_TTL_SECONDS", 1800))
    return max(30, _env_int("OCR_GEMINI_WARMUP_TTL_SECONDS", 600))


def _smart_refine_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_SMART_REFINE", False)


def _smart_refine_max_pages() -> int:
    return max(0, _env_int("OCR_SMART_REFINE_MAX_PAGES", 1))


def _hq_refine_dpi() -> int:
    return max(120, _env_int("OCR_GEMINI_HQ_DPI", 240))


def _hq_refine_format() -> str:
    normalized = os.environ.get("OCR_GEMINI_HQ_FORMAT", "jpeg").strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpeg"
    if normalized == "png":
        return "png"
    return "jpeg"


def _hq_refine_jpeg_quality() -> int:
    return min(95, max(60, _env_int("OCR_GEMINI_HQ_JPEG_QUALITY", 88)))


def _allow_low_row_gemini_result() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ALLOW_LOW_ROW_GEMINI", False)


def _use_gemini() -> bool:
    return _env_flag("OCR_USE_GEMINI", True)


def _use_documentai() -> bool:
    return _env_flag("OCR_USE_DOCUMENTAI", True)


def _force_page_by_page() -> bool:
    if _stable_production_mode_enabled():
        return True
    return _env_flag("OCR_FORCE_PAGE_BY_PAGE", True)


def _remote_crop_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_REMOTE_CROP", False)


def _expected_name_count_enabled() -> bool:
    if _stable_production_mode_enabled():
        return False
    return _env_flag("OCR_ENABLE_EXPECTED_NAME_COUNT", False)


def _documentai_fallback_available() -> tuple[bool, str]:
    global _DOCAI_FALLBACK_AVAILABLE, _DOCAI_FALLBACK_REASON
    if _DOCAI_FALLBACK_AVAILABLE is not None:
        return _DOCAI_FALLBACK_AVAILABLE, _DOCAI_FALLBACK_REASON

    try:
        from documentai_extractor import is_documentai_runtime_ready

        ready, reason = is_documentai_runtime_ready()
        _DOCAI_FALLBACK_AVAILABLE = ready
        _DOCAI_FALLBACK_REASON = reason
    except Exception as exc:
        _DOCAI_FALLBACK_AVAILABLE = False
        _DOCAI_FALLBACK_REASON = str(exc)
    return _DOCAI_FALLBACK_AVAILABLE, _DOCAI_FALLBACK_REASON


def _batch_size_for_profile(profile: ProcessingProfile, page_count: int) -> int:
    configured = _env_int("OCR_GEMINI_BATCH_SIZE", 0)
    if configured > 0:
        return max(1, min(configured, page_count))
    if profile.name == "small":
        return min(2, page_count)
    if profile.name == "medium":
        return min(3, page_count)
    return min(4, page_count)


def inspect_pdf_document(file_path: Path) -> tuple[int, str]:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    if file_path.suffix.lower() == ".pdf":
        return count_pdf_pages(file_path), mime_type
    return 1, mime_type


def select_processing_profile(file_path: Path, page_count: int, file_size_bytes: int) -> ProcessingProfile:
    del file_path
    size_mb = file_size_bytes / (1024 * 1024)
    if _stable_production_mode_enabled():
        max_concurrency_cap = max(1, _env_int("OCR_GEMINI_STABLE_MAX_CONCURRENCY", 4))
        small_dpi = 180
        medium_dpi = 160
        large_dpi = 140
    else:
        max_concurrency_cap = max(1, _env_int("OCR_GEMINI_MAX_CONCURRENCY", 6))
        small_dpi = _env_int("OCR_GEMINI_SMALL_DPI", 180)
        medium_dpi = _env_int("OCR_GEMINI_MEDIUM_DPI", 130)
        large_dpi = _env_int("OCR_GEMINI_LARGE_DPI", 120)
    min_rows_override = _env_int("OCR_GEMINI_MIN_ROWS_PER_PAGE", 0)

    if page_count <= 4 and size_mb <= 6:
        return ProcessingProfile(
            name="small",
            dpi=small_dpi,
            image_format="jpeg",
            jpeg_quality=82,
            max_concurrency=min(4, max_concurrency_cap),
            min_rows_per_page=min_rows_override or 8,
            fallback_mode="documentai",
        )
    if page_count <= 12 and size_mb <= 15:
        return ProcessingProfile(
            name="medium",
            dpi=medium_dpi,
            image_format="jpeg",
            jpeg_quality=75,
            max_concurrency=min(6, max_concurrency_cap),
            min_rows_per_page=min_rows_override or 8,
            fallback_mode="documentai",
        )
    return ProcessingProfile(
        name="large",
        dpi=large_dpi,
        image_format="jpeg",
        jpeg_quality=68,
        max_concurrency=min(6, max_concurrency_cap),
        min_rows_per_page=min_rows_override or 6,
        fallback_mode="documentai",
    )


def render_pdf_for_profile(file_path: Path, output_dir: Path, profile: ProcessingProfile) -> list[Path]:
    if file_path.suffix.lower() != ".pdf":
        return [file_path]
    return pdf_to_images(
        file_path,
        output_dir,
        dpi=profile.dpi,
        image_format=profile.image_format,
        jpeg_quality=profile.jpeg_quality,
    )


def process_page_with_gemini(
    image_path: Path,
    lang: str = "pt",
    model_name: str | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
) -> dict[str, Any]:
    from gemini_extractor import process_page_with_gemini as run_gemini_page

    return run_gemini_page(
        image_path,
        lang=lang,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        retries=retries,
    )


def process_pages_with_gemini(
    image_paths: list[Path],
    lang: str = "pt",
    model_name: str | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
) -> list[dict[str, Any]]:
    from gemini_extractor import process_pages_with_gemini as run_gemini_batch

    return run_gemini_batch(
        image_paths,
        lang=lang,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        retries=retries,
    )


def warmup_gemini_runtime(timeout_seconds: int = 8) -> dict[str, Any]:
    from gemini_extractor import warmup_gemini_runtime as run_gemini_warmup

    return run_gemini_warmup(timeout_seconds=timeout_seconds)


def maybe_warmup_gemini_runtime(timeout_seconds: int = 8) -> dict[str, Any]:
    global _GEMINI_WARMUP_CACHE, _GEMINI_WARMUP_CACHE_TS

    now = time.perf_counter()
    ttl_seconds = _gemini_warmup_ttl_seconds()
    if _GEMINI_WARMUP_CACHE is not None and (now - _GEMINI_WARMUP_CACHE_TS) <= ttl_seconds:
        cached = dict(_GEMINI_WARMUP_CACHE)
        cached["cached"] = True
        return cached

    warmup_info = warmup_gemini_runtime(timeout_seconds=timeout_seconds)
    _GEMINI_WARMUP_CACHE = dict(warmup_info)
    _GEMINI_WARMUP_CACHE_TS = now
    warmup_info["cached"] = False
    return warmup_info


def process_page_with_documentai(image_path: Path, lang: str = "pt", page_number: int = 1) -> list[ExtractedRow]:
    from documentai_extractor import extract_attendance_page_documentai

    return extract_attendance_page_documentai(image_path, page_number=page_number, lang=lang)


def _crop_remote_image_if_possible(image_path: Path) -> Path:
    if not _remote_crop_enabled():
        return image_path
    try:
        from PIL import Image
        from assinatura_lista import _auto_rotate, detect_table_grid
    except Exception:
        return image_path

    try:
        image = Image.open(image_path).convert("RGB")
        image = _auto_rotate(image)
        grid = detect_table_grid(image)
        if not grid or not grid.horizontal or not grid.vertical:
            return image_path

        left = max(0, min(grid.vertical) - 24)
        right = min(image.width, max(grid.vertical) + 24)
        top = max(0, min(grid.horizontal) - 80)
        bottom = min(image.height, max(grid.horizontal) + 80)
        if right - left < image.width * 0.55 or bottom - top < image.height * 0.55:
            return image_path

        cropped = image.crop((left, top, right, bottom))
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            cropped.save(image_path, format="JPEG", quality=75, optimize=True)
        else:
            cropped.save(image_path, format="PNG", optimize=True)
        return image_path
    except Exception:
        return image_path


def _estimate_expected_name_count(image_path: Path) -> int:
    if not _expected_name_count_enabled():
        return 0
    try:
        from PIL import Image
        from assinatura_lista import _auto_rotate, data_row_intervals, detect_table_grid
    except Exception:
        return 0

    try:
        image = Image.open(image_path).convert("RGB")
        image = _auto_rotate(image)
        grid = detect_table_grid(image)
        if not grid:
            return 0
        return len(data_row_intervals(grid.horizontal))
    except Exception:
        return 0


def _clean_header_value(value: Any) -> str:
    cleaned = str(value or "").strip()
    if cleaned.upper() in {"N/A", "NA", "NONE", "NULL", "-", "--"}:
        return ""
    return cleaned


def _normalize_header_dict(header: dict[str, Any]) -> dict[str, str]:
    normalized = {key: _clean_header_value(header.get(key, "")) for key in _HEADER_KEYS}

    modulo = normalized["modulo"]
    modulo_match = re.search(r"modulo\s*([ivxlcdm0-9]+)", modulo, re.IGNORECASE)
    if modulo_match:
        normalized["modulo"] = f"MÓDULO {modulo_match.group(1).upper()}"
    elif modulo.upper().startswith("MÓDULO "):
        normalized["modulo"] = modulo.upper()

    curso = normalized["curso"]
    if curso:
        curso = re.sub(r"^(curso\s+de\s+forma[çc][aã]o\s+em)\s*", "", curso, flags=re.IGNORECASE)
        normalized["curso"] = f"CURSO DE FORMAÇÃO EM {curso}".strip()

    turma = normalized["turma"]
    if turma:
        turma_match = re.search(r"(\d+[A-Z]?)", turma, re.IGNORECASE)
        if turma_match:
            normalized["turma"] = f"Turma {turma_match.group(1)}"

    return normalized


def _normalize_person_name(value: Any) -> str:
    return str(value or "").strip()


def _is_placeholder_name(value: Any) -> bool:
    normalized = _normalize_person_name(value).lower()
    return normalized in {"(sem nome impresso)", "sem nome impresso", "(sem nome)", "n/a"}


def _named_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _normalize_person_name(row.get("nome")) and not _is_placeholder_name(row.get("nome")))


def _placeholder_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _is_placeholder_name(row.get("nome")))


def _present_cell_count(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        for prefix in ("matutino", "vespertino", "noturno"):
            status = str(row.get(f"{prefix}_status", "")).strip().lower()
            if status in {"presente", "sim", "yes", "p", "ok", "x"}:
                count += 1
    return count


def _named_row_has_any_presence(row: dict[str, Any]) -> bool:
    for prefix in ("matutino", "vespertino", "noturno"):
        status = str(row.get(f"{prefix}_status", "")).strip().lower()
        if status in {"presente", "sim", "yes", "p", "ok", "x"}:
            return True
    return False


def _sparse_page_needs_retry(rows: list[dict[str, Any]]) -> bool:
    named_rows = [
        row for row in rows
        if _normalize_person_name(row.get("nome")) and not _is_placeholder_name(row.get("nome"))
    ]
    if not named_rows:
        return True
    return any(not _named_row_has_any_presence(row) for row in named_rows)


def _nonempty_text_count(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        for prefix in ("matutino", "vespertino", "noturno"):
            if str(row.get(f"{prefix}_texto", "")).strip():
                count += 1
    return count


def _suspicious_present_cell_count(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        printed_name = row.get("nome", "")
        for prefix in ("matutino", "vespertino", "noturno"):
            status = str(row.get(f"{prefix}_status", "")).strip().lower()
            text = row.get(f"{prefix}_texto", "")
            if status in {"presente", "sim", "yes", "p", "ok", "x"} and _is_suspicious_present_text(printed_name, text):
                count += 1
    return count


def _is_repeated_simple_marker(values: list[str]) -> bool:
    normalized = [value.strip().lower() for value in values if value.strip()]
    if len(normalized) < 2:
        return False
    return len(set(normalized)) == 1 and normalized[0] in {"sim", "ok", "x", "presente"}


def _is_suspicious_present_row(row: dict[str, Any]) -> bool:
    printed_name = row.get("nome", "")
    present_texts: list[str] = []
    suspicious_cells = 0
    for prefix in ("matutino", "vespertino", "noturno"):
        status = str(row.get(f"{prefix}_status", "")).strip().lower()
        text = str(row.get(f"{prefix}_texto", "")).strip()
        if status not in {"presente", "sim", "yes", "p", "ok", "x"}:
            continue
        present_texts.append(text)
        if _is_suspicious_present_text(printed_name, text):
            suspicious_cells += 1

    if suspicious_cells >= 2:
        return True
    if len(present_texts) == 3 and _is_repeated_simple_marker(present_texts):
        return True
    return False


def _gemini_result_score(header: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[int, int, int, int, int]:
    normalized_header = _normalize_header_dict(header)
    return (
        _named_row_count(rows),
        -_placeholder_row_count(rows),
        _present_cell_count(rows),
        _nonempty_text_count(rows),
        sum(1 for key in _HEADER_KEYS if normalized_header.get(key)),
    )


def _page_named_row_count(page_result: AttendancePageResult) -> int:
    return _named_row_count(page_result.rows)


def _should_retry_gemini_with_high_quality(
    header: dict[str, Any],
    rows: list[dict[str, Any]],
    profile: ProcessingProfile,
    expected_name_count: int = 0,
) -> bool:
    named_count = _named_row_count(rows)
    if named_count == 0:
        return True
    if expected_name_count >= 10 and named_count < max(1, expected_name_count - 2):
        return True
    if _placeholder_row_count(rows) > 0:
        return True
    if len(rows) < profile.min_rows_per_page:
        return _sparse_page_needs_retry(rows)
    normalized_header = _normalize_header_dict(header)
    if not any(normalized_header.values()) and named_count > 2:
        return True
    return False


def _needs_strong_model_escalation(
    header: dict[str, Any],
    rows: list[dict[str, Any]],
    profile: ProcessingProfile,
    expected_name_count: int = 0,
) -> bool:
    if _can_accept_gemini_result(
        rows,
        profile,
        allow_low_row_count=False,
        expected_name_count=expected_name_count,
    ):
        return False
    if _placeholder_row_count(rows) > 0:
        return True
    if _suspicious_present_cell_count(rows) >= 2:
        return True
    normalized_header = _normalize_header_dict(header)
    return not any(normalized_header.values()) and _named_row_count(rows) >= max(10, profile.min_rows_per_page)


def _can_accept_gemini_result(
    rows: list[dict[str, Any]],
    profile: ProcessingProfile,
    *,
    allow_low_row_count: bool,
    expected_name_count: int = 0,
) -> bool:
    named_count = _named_row_count(rows)
    if named_count == 0:
        return False
    if expected_name_count >= 10 and named_count < max(1, expected_name_count - 2):
        return False
    if _placeholder_row_count(rows) > 0:
        return False
    if allow_low_row_count:
        return _allow_low_row_gemini_result()
    return len(rows) >= profile.min_rows_per_page


def _name_key(value: Any) -> str:
    normalized = _normalize_person_name(value)
    if not normalized:
        return ""
    decomposed = unicodedata.normalize("NFKD", normalized)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", without_accents).strip().lower()


def _looks_like_empty_mark(text: Any) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    compact = re.sub(r"\s+", "", value)
    return bool(compact) and all(char in "-_=./\\|~`" for char in compact)


def _looks_like_same_as_printed_name(printed_name: Any, text: Any) -> bool:
    printed_key = _name_key(printed_name)
    text_key = _name_key(text)
    if not printed_key or not text_key:
        return False
    if printed_key == text_key:
        return True

    printed_tokens = [token for token in re.split(r"\s+", printed_key) if token]
    text_tokens = [token for token in re.split(r"\s+", text_key) if token]
    printed_token_set = {
        re.sub(r"[^a-z0-9]", "", token)
        for token in printed_tokens
        if re.sub(r"[^a-z0-9]", "", token)
    }
    text_token_set = [
        re.sub(r"[^a-z0-9]", "", token)
        for token in text_tokens
        if re.sub(r"[^a-z0-9]", "", token)
    ]
    if len(text_tokens) == 1 and len(text_tokens[0]) >= 5:
        token = text_tokens[0]
        if token in printed_tokens:
            return True
        if printed_key.startswith(token):
            return True

    significant_tokens = [token for token in text_token_set if len(token) >= 4]
    if significant_tokens and all(token in printed_token_set for token in significant_tokens):
        return True

    compact_printed = printed_key.replace(" ", "")
    compact_text = text_key.replace(" ", "")
    if len(compact_text) >= 5 and compact_printed.startswith(compact_text):
        return True
    return False


def _is_suspicious_present_text(printed_name: Any, text: Any) -> bool:
    normalized_text = str(text or "").strip().lower()
    if normalized_text in {"sim", "ok", "x", "presente"}:
        return True
    if _looks_like_empty_mark(text):
        return True
    if _looks_like_same_as_printed_name(printed_name, text):
        return True
    return False


def _period_value_score(status: Any, text: Any, explicit_type: Any) -> tuple[int, int, int]:
    normalized_status = str(status or "").strip().lower()
    normalized_text = str(text or "").strip()
    normalized_type = str(explicit_type or "").strip()
    present_score = 1 if normalized_status in {"presente", "sim", "yes", "p", "ok", "x"} else 0
    text_score = len(normalized_text)
    type_score = 1 if normalized_type else 0
    return (present_score, text_score, type_score)


def _merge_logical_row_values(base_row: dict[str, Any], candidate_row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_row)

    base_name = _normalize_person_name(base_row.get("nome"))
    candidate_name = _normalize_person_name(candidate_row.get("nome"))
    if len(candidate_name) > len(base_name):
        merged["nome"] = candidate_name

    for key in _HEADER_KEYS:
        if not _clean_header_value(merged.get(key)):
            merged[key] = candidate_row.get(key, "")

    for prefix in ("matutino", "vespertino", "noturno"):
        current_score = _period_value_score(
            merged.get(f"{prefix}_status", ""),
            merged.get(f"{prefix}_texto", ""),
            merged.get(f"{prefix}_tipo", ""),
        )
        candidate_score = _period_value_score(
            candidate_row.get(f"{prefix}_status", ""),
            candidate_row.get(f"{prefix}_texto", ""),
            candidate_row.get(f"{prefix}_tipo", ""),
        )
        if candidate_score > current_score:
            merged[f"{prefix}_status"] = candidate_row.get(f"{prefix}_status", "")
            merged[f"{prefix}_texto"] = candidate_row.get(f"{prefix}_texto", "")
            if candidate_row.get(f"{prefix}_tipo", ""):
                merged[f"{prefix}_tipo"] = candidate_row.get(f"{prefix}_tipo", "")

    return merged


def _merge_gemini_rows(primary_rows: list[dict[str, Any]], secondary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_rows: list[dict[str, Any]] = []
    key_to_index: dict[str, int] = {}

    for row in primary_rows + secondary_rows:
        name = _normalize_person_name(row.get("nome"))
        if not name or _is_placeholder_name(name):
            continue
        key = _name_key(name)
        if not key:
            continue
        if key in key_to_index:
            index = key_to_index[key]
            merged_rows[index] = _merge_logical_row_values(merged_rows[index], row)
            continue
        key_to_index[key] = len(merged_rows)
        merged_rows.append(dict(row))

    return merged_rows


def _apply_visual_absence_guard(image_path: Path, rows: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    if len(rows) < 10:
        return rows

    try:
        from PIL import Image
        from assinatura_lista import (
            _auto_rotate,
            analyze_cell_ink,
            crop_with_margin,
            data_row_intervals,
            detect_table_grid,
            signature_column_intervals,
        )
    except Exception:
        return rows

    try:
        image = Image.open(image_path).convert("RGB")
        image = _auto_rotate(image)
        grid = detect_table_grid(image)
        if not grid:
            return rows
        row_intervals = data_row_intervals(grid.horizontal)
        if not row_intervals or len(rows) < max(10, len(row_intervals) // 2):
            return rows
        signature_cols = signature_column_intervals(grid.vertical, image.width)
        if len(signature_cols) < 3:
            return rows
    except Exception:
        return rows

    validated_rows = [dict(row) for row in rows]
    row_offset = max(0, len(row_intervals) - len(validated_rows))
    max_rows = min(len(validated_rows), len(row_intervals) - row_offset)
    for row_index in range(max_rows):
        logical_row = validated_rows[row_index]
        top, bottom = row_intervals[row_index + row_offset]
        printed_name = logical_row.get("nome", "")
        for prefix, (left, right) in zip(("matutino", "vespertino", "noturno"), signature_cols):
            status_key = f"{prefix}_status"
            text_key = f"{prefix}_texto"
            tipo_key = f"{prefix}_tipo"
            status = str(logical_row.get(status_key, "")).strip().lower()
            text = str(logical_row.get(text_key, "")).strip()
            if status not in {"presente", "sim", "yes", "p", "ok", "x"}:
                continue
            if not _is_suspicious_present_text(printed_name, text):
                continue

            cell = crop_with_margin(image, left, top, right, bottom, 4)
            ink = analyze_cell_ink(cell, "por+eng" if lang == "pt" else lang, include_text=False)
            if not ink.signed:
                logical_row[status_key] = "Ausente"
                logical_row[text_key] = ""
                logical_row[tipo_key] = ""
            elif ink.text and _looks_like_same_as_printed_name(printed_name, text):
                logical_row[text_key] = ink.text.strip()

    return validated_rows


def _apply_visual_presence_guard(image_path: Path, rows: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    if len(rows) < 8:
        return rows

    try:
        from PIL import Image
        from assinatura_lista import (
            _auto_rotate,
            analyze_cell_ink,
            crop_with_margin,
            data_row_intervals,
            detect_table_grid,
            signature_column_intervals,
        )
    except Exception:
        return rows

    try:
        image = Image.open(image_path).convert("RGB")
        image = _auto_rotate(image)
        grid = detect_table_grid(image)
        if not grid:
            return rows
        row_intervals = data_row_intervals(grid.horizontal)
        if not row_intervals:
            return rows
        signature_cols = signature_column_intervals(grid.vertical, image.width)
        if len(signature_cols) < 3:
            return rows
    except Exception:
        return rows

    validated_rows = [dict(row) for row in rows]
    row_offset = max(0, len(row_intervals) - len(validated_rows))
    max_rows = min(len(validated_rows), len(row_intervals) - row_offset)
    lang_for_ink = "por+eng" if lang == "pt" else lang
    for row_index in range(max_rows):
        logical_row = validated_rows[row_index]
        top, bottom = row_intervals[row_index + row_offset]
        for prefix, (left, right) in zip(("matutino", "vespertino", "noturno"), signature_cols):
            status_key = f"{prefix}_status"
            text_key = f"{prefix}_texto"
            tipo_key = f"{prefix}_tipo"
            status = str(logical_row.get(status_key, "")).strip().lower()
            if status not in {"presente", "sim", "yes", "p", "ok", "x"}:
                continue
            cell = crop_with_margin(image, left, top, right, bottom, 4)
            ink = analyze_cell_ink(cell, lang_for_ink, include_text=False)
            if not ink.signed:
                logical_row[status_key] = "Ausente"
                logical_row[text_key] = ""
                logical_row[tipo_key] = ""
                continue
            current_text = str(logical_row.get(text_key, "")).strip()
            if not current_text and ink.text:
                logical_row[text_key] = ink.text.strip()

    return validated_rows


def _suspicious_present_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _is_suspicious_present_row(row))


def _should_run_visual_absence_guard(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 10:
        return False
    suspicious_rows = _suspicious_present_row_count(rows)
    suspicious_cells = _suspicious_present_cell_count(rows)
    present_cells = _present_cell_count(rows)
    if suspicious_rows >= 2:
        return True
    if suspicious_rows >= 1 and suspicious_cells >= 3 and present_cells >= 6:
        return True
    return False


def _batch_result_needs_single_page_retry(rows: list[dict[str, Any]], expected_name_count: int) -> bool:
    if not rows:
        return True
    suspicious_rows = _suspicious_present_row_count(rows)
    if suspicious_rows >= 2:
        return True
    if expected_name_count >= 10 and suspicious_rows >= 1 and _suspicious_present_cell_count(rows) >= 3:
        return True
    return False


def _rows_far_above_expected(rows: list[dict[str, Any]], expected_name_count: int) -> bool:
    if expected_name_count <= 0:
        return False
    named_count = _named_row_count(rows)
    tolerance = max(3, expected_name_count // 2)
    return named_count > expected_name_count + tolerance


def _apply_legacy_signature_guard(
    image_path: Path,
    source_path: Path,
    page_number: int,
    rows: list[dict[str, Any]],
    lang: str,
) -> list[dict[str, Any]]:
    if len(rows) < 10 or _suspicious_present_cell_count(rows) < 2:
        return rows

    try:
        from assinatura_lista import analyze_attendance_image
    except Exception:
        return rows

    try:
        legacy_rows, _ = analyze_attendance_image(image_path, source_path, page_number, "por+eng" if lang == "pt" else lang)
        _, legacy_logical_rows = _logical_rows_from_extracted_rows(legacy_rows)
    except Exception:
        return rows

    if not legacy_logical_rows:
        return rows

    legacy_by_name = {
        _name_key(row.get("nome", "")): row
        for row in legacy_logical_rows
        if _name_key(row.get("nome", ""))
    }

    validated_rows = [dict(row) for row in rows]
    for row_index, logical_row in enumerate(validated_rows):
        printed_name = logical_row.get("nome", "")
        legacy_row = legacy_by_name.get(_name_key(printed_name))
        if not legacy_row and row_index < len(legacy_logical_rows):
            legacy_row = legacy_logical_rows[row_index]
        if not legacy_row:
            continue
        if _is_suspicious_present_row(logical_row):
            for prefix in ("matutino", "vespertino", "noturno"):
                status_key = f"{prefix}_status"
                text_key = f"{prefix}_texto"
                tipo_key = f"{prefix}_tipo"
                logical_row[status_key] = legacy_row.get(status_key, logical_row.get(status_key, ""))
                logical_row[text_key] = legacy_row.get(text_key, logical_row.get(text_key, ""))
                logical_row[tipo_key] = legacy_row.get(tipo_key, logical_row.get(tipo_key, ""))
            continue
        for prefix in ("matutino", "vespertino", "noturno"):
            status_key = f"{prefix}_status"
            text_key = f"{prefix}_texto"
            tipo_key = f"{prefix}_tipo"
            status = str(logical_row.get(status_key, "")).strip().lower()
            text = logical_row.get(text_key, "")
            if status not in {"presente", "sim", "yes", "p", "ok", "x"}:
                continue
            if not _is_suspicious_present_text(printed_name, text):
                continue

            legacy_status = str(legacy_row.get(status_key, "")).strip().lower()
            if legacy_status not in {"presente", "sim", "yes", "p", "ok", "x"}:
                logical_row[status_key] = "Ausente"
                logical_row[text_key] = ""
                logical_row[tipo_key] = ""
                continue

            legacy_text = str(legacy_row.get(text_key, "")).strip()
            legacy_type = str(legacy_row.get(tipo_key, "")).strip()
            if legacy_text and not _looks_like_same_as_printed_name(printed_name, legacy_text):
                logical_row[text_key] = legacy_text
                if legacy_type:
                    logical_row[tipo_key] = legacy_type

    return validated_rows


def _refine_result_with_high_quality(
    source_path: Path,
    page_result: AttendancePageResult,
    lang: str,
) -> AttendancePageResult:
    if source_path.suffix.lower() != ".pdf":
        return page_result

    page_number = page_result.page_number
    try:
        with tempfile.TemporaryDirectory(prefix=f"attendance_gemini_refine_{page_number:04d}_") as temp_dir:
            hq_format = _hq_refine_format()
            hq_extension = "png" if hq_format == "png" else "jpg"
            hq_image_path = Path(temp_dir) / f"page_{page_number:04d}.{hq_extension}"
            render_pdf_page(
                source_path,
                page_number,
                hq_image_path,
                dpi=_hq_refine_dpi(),
                image_format=hq_format,
                jpeg_quality=_hq_refine_jpeg_quality(),
            )
            refine_start = time.perf_counter()
            prepared_hq_image_path = _crop_remote_image_if_possible(hq_image_path)
            high_quality_result = process_page_with_gemini(prepared_hq_image_path, lang=lang)
            refine_ms = int((time.perf_counter() - refine_start) * 1000)
    except Exception:
        return page_result

    hq_rows = list(high_quality_result.get("rows", []))
    hq_header = _normalize_header_dict(dict(high_quality_result.get("header", {})))
    merged_rows = _merge_gemini_rows(page_result.rows, hq_rows)
    merged_header = _merge_headers(page_result.header, hq_header)

    best_rows = page_result.rows
    best_header = page_result.header
    best_processor = page_result.processor_used

    if _gemini_result_score(merged_header, merged_rows) >= _gemini_result_score(best_header, best_rows):
        best_rows = merged_rows
        best_header = merged_header
        best_processor = "gemini_merged"

    if _gemini_result_score(hq_header, hq_rows) > _gemini_result_score(best_header, best_rows):
        best_rows = hq_rows
        best_header = hq_header
        best_processor = "gemini_hq"

    updated_timings = dict(page_result.timings_ms)
    updated_timings["gemini_ms"] = updated_timings.get("gemini_ms", 0) + refine_ms
    return AttendancePageResult(
        page_number=page_number,
        rows=best_rows,
        header=best_header,
        processor_used=best_processor,
        timings_ms=updated_timings,
    )


def _page_needs_smart_refine(page_result: AttendancePageResult, profile: ProcessingProfile) -> bool:
    if not page_result.processor_used.startswith("gemini"):
        return False
    rows = page_result.rows
    named_count = _named_row_count(rows)
    if named_count == 0:
        return True
    placeholder_count = _placeholder_row_count(rows)
    suspicious_count = _suspicious_present_cell_count(rows)
    if placeholder_count > 0:
        return True
    if suspicious_count >= 2:
        return True
    return False


def _smart_refine_priority(page_result: AttendancePageResult, profile: ProcessingProfile) -> tuple[int, int, int, int]:
    rows = page_result.rows
    named_count = _named_row_count(rows)
    missing_rows = max(0, profile.min_rows_per_page - len(rows))
    return (
        _placeholder_row_count(rows) + _suspicious_present_cell_count(rows),
        missing_rows,
        max(0, 3 - named_count),
        -page_result.page_number,
    )


def _merge_headers(base: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    return _normalize_header_dict({
        "modulo": current.get("modulo") or base.get("modulo", ""),
        "curso": current.get("curso") or base.get("curso", ""),
        "turma": current.get("turma") or base.get("turma", ""),
        "data": current.get("data") or base.get("data", ""),
    })


def _normalize_period_output(status: str, text: str, explicit_type: str = "") -> tuple[str, str, str]:
    normalized_status = (status or "Ausente").strip().lower()
    cleaned_text = (text or "").strip()

    if normalized_status in {"presente", "sim", "yes", "p", "ok", "x"}:
        presenca = "Presente"
    else:
        presenca = "Ausente"
        cleaned_text = ""

    if presenca == "Ausente":
        return presenca, "nao_assinado", ""

    # Validacao extra: texto que parece lixo/estrutura da tabela
    if cleaned_text:
        stripped = cleaned_text.strip("-–—._|/\\() \t")
        if not stripped or len(stripped) <= 1:
            cleaned_text = ""

    # Se o Gemini forneceu o tipo diretamente, confiar nele
    if explicit_type and explicit_type in {"nao_assinado", "marcacao", "rubrica", "nome_manuscrito"}:
        # Mas se o tipo e nao_assinado e o status e Presente, ha inconsistencia
        if explicit_type == "nao_assinado":
            return "Ausente", "nao_assinado", ""
        return presenca, explicit_type, cleaned_text

    # Classificacao de tipo por heuristica (fallback quando Gemini nao fornece tipo)
    text_lower = cleaned_text.lower().strip()

    # Marcacao: "Sim", "OK", "X", variantes
    if text_lower in {"sim", "ok", "x", "ok! sim", "ok sim", "v", "visto", "presente", "yes"}:
        return presenca, "marcacao", "Sim"

    # Sem texto = rubrica
    if not cleaned_text:
        return presenca, "rubrica", ""

    # Texto muito curto (1-4 chars) = rubrica
    if len(cleaned_text) <= 4:
        return presenca, "rubrica", cleaned_text

    # Classifica como nome_manuscrito apenas se claramente um nome legivel:
    # - 2+ palavras com pelo menos uma capitalized e >= 4 chars
    words = cleaned_text.split()
    capitalized_words = sum(1 for w in words if w and w[0].isupper() and len(w) >= 4)

    if len(words) >= 2 and capitalized_words >= 2:
        return presenca, "nome_manuscrito", cleaned_text

    # Texto longo com multiplas palavras = nome_manuscrito
    if len(words) >= 2 and len(cleaned_text) > 10:
        return presenca, "nome_manuscrito", cleaned_text

    # Texto longo de uma palavra so (>= 12 chars) = nome_manuscrito
    if len(cleaned_text) >= 12 and len(words) == 1:
        return presenca, "nome_manuscrito", cleaned_text

    # Tudo o resto = rubrica (mais conservador)
    return presenca, "rubrica", cleaned_text


def _logical_rows_from_documentai(rows: list[ExtractedRow]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    return _logical_rows_from_extracted_rows(rows)


def _logical_rows_from_extracted_rows(rows: list[ExtractedRow]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not rows:
        return {"modulo": "", "curso": "", "turma": "", "data": ""}, []

    logical_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    for row in rows:
        columns = list(row.columns) + [""] * max(0, 9 - len(row.columns))
        modulo, curso, turma, data, nome, periodo, presenca, tipo, texto = columns[:9]
        key = (modulo, curso, turma, data, nome)
        if key not in grouped:
            grouped[key] = {
                "modulo": modulo,
                "curso": curso,
                "turma": turma,
                "data": data,
                "nome": nome,
                "matutino_status": "Ausente",
                "matutino_texto": "",
                "matutino_tipo": "",
                "vespertino_status": "Ausente",
                "vespertino_texto": "",
                "vespertino_tipo": "",
                "noturno_status": "Ausente",
                "noturno_texto": "",
                "noturno_tipo": "",
            }
            logical_rows.append(grouped[key])

        prefix = periodo.strip().lower()
        if prefix not in {"matutino", "vespertino", "noturno"}:
            continue
        grouped[key][f"{prefix}_status"] = presenca
        grouped[key][f"{prefix}_texto"] = texto
        grouped[key][f"{prefix}_tipo"] = tipo

    first = logical_rows[0]
    header = {
        "modulo": str(first.get("modulo", "")),
        "curso": str(first.get("curso", "")),
        "turma": str(first.get("turma", "")),
        "data": str(first.get("data", "")),
    }
    return header, logical_rows


def _build_page_result_from_documentai(
    page_number: int,
    docai_rows: list[ExtractedRow],
    fallback_ms: int,
) -> AttendancePageResult:
    header, logical_rows = _logical_rows_from_documentai(docai_rows)
    return AttendancePageResult(
        page_number=page_number,
        rows=logical_rows,
        header=_normalize_header_dict(header),
        processor_used="documentai",
        timings_ms={"fallback_ms": fallback_ms},
    )


def _build_page_result_from_legacy(
    source_path: Path,
    image_path: Path,
    page_number: int,
    lang: str,
) -> AttendancePageResult | None:
    try:
        from assinatura_lista import analyze_attendance_image
    except Exception:
        return None


def _dedupe_page_rows_by_name(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_rows: list[dict[str, Any]] = []
    key_to_index: dict[str, int] = {}
    for row in rows:
        name = _normalize_person_name(row.get("nome"))
        if not name:
            continue
        key = _name_key(name)
        if not key:
            continue
        if key in key_to_index:
            idx = key_to_index[key]
            merged_rows[idx] = _merge_logical_row_values(merged_rows[idx], row)
            continue
        key_to_index[key] = len(merged_rows)
        merged_rows.append(dict(row))
    return merged_rows


def _trim_rows_to_expected_count(rows: list[dict[str, Any]], expected_name_count: int) -> list[dict[str, Any]]:
    if expected_name_count <= 0:
        return rows
    deduped = _dedupe_page_rows_by_name(rows)
    if len(deduped) <= expected_name_count + 1:
        return deduped

    def score(row: dict[str, Any]) -> tuple[int, int, int]:
        present_cells = 0
        text_cells = 0
        for prefix in ("matutino", "vespertino", "noturno"):
            status = str(row.get(f"{prefix}_status", "")).strip().lower()
            if status in {"presente", "sim", "yes", "p", "ok", "x"}:
                present_cells += 1
            if str(row.get(f"{prefix}_texto", "")).strip():
                text_cells += 1
        name_len = len(_normalize_person_name(row.get("nome")))
        return (present_cells, text_cells, name_len)

    ordered = sorted(enumerate(deduped), key=lambda item: (score(item[1]), -item[0]), reverse=True)
    selected_idx = set(index for index, _ in ordered[:expected_name_count])
    return [row for idx, row in enumerate(deduped) if idx in selected_idx]

    try:
        legacy_rows, _ = analyze_attendance_image(
            image_path,
            source_path,
            page_number,
            "por+eng" if lang == "pt" else lang,
        )
        header, logical_rows = _logical_rows_from_extracted_rows(legacy_rows)
        if not logical_rows:
            return None
        return AttendancePageResult(
            page_number=page_number,
            rows=logical_rows,
            header=_normalize_header_dict(header),
            processor_used="legacy_ocr",
            timings_ms={},
        )
    except Exception:
        return None


def _process_single_page(
    source_path: Path,
    page_number: int,
    image_path: Path,
    profile: ProcessingProfile,
    lang: str,
    prefetched_gemini_result: dict[str, Any] | None = None,
    prefetched_gemini_ms: int = 0,
    expected_name_count_override: int | None = None,
) -> AttendancePageResult:
    gemini_error: Exception | None = None
    gemini_ms = prefetched_gemini_ms
    expected_name_count = expected_name_count_override if expected_name_count_override is not None else _estimate_expected_name_count(image_path)
    partial_gemini_result: AttendancePageResult | None = None
    primary_model_name = _fast_model_name() if _fast_model_first_pass_enabled() else _strong_model_name()
    primary_timeout = _fast_model_timeout_seconds() if _fast_model_first_pass_enabled() else _strong_model_timeout_seconds()
    primary_retries = _fast_model_retries() if _fast_model_first_pass_enabled() else _strong_model_retries()
    strong_model_name = _strong_model_name()
    can_escalate_to_strong = _fast_model_first_pass_enabled() and primary_model_name != strong_model_name

    if _use_gemini():
        try:
            if prefetched_gemini_result is None:
                gemini_start = time.perf_counter()
                prepared_image_path = _crop_remote_image_if_possible(image_path)
                gemini_result = process_page_with_gemini(
                    prepared_image_path,
                    lang=lang,
                    model_name=primary_model_name,
                    timeout_seconds=primary_timeout,
                    retries=primary_retries,
                )
                gemini_ms = int((time.perf_counter() - gemini_start) * 1000)
            else:
                prepared_image_path = image_path
                gemini_result = prefetched_gemini_result
            rows = list(gemini_result.get("rows", []))
            if _absence_guard_enabled() and _should_run_visual_absence_guard(rows):
                rows = _apply_visual_absence_guard(prepared_image_path, rows, lang)
            if _legacy_guard_enabled():
                rows = _apply_legacy_signature_guard(prepared_image_path, source_path, page_number, rows, lang)
            if _presence_guard_enabled():
                rows = _apply_visual_presence_guard(prepared_image_path, rows, lang)
            rows = _trim_rows_to_expected_count(rows, expected_name_count)
            if prefetched_gemini_result is not None and _batch_result_needs_single_page_retry(rows, expected_name_count):
                gemini_start = time.perf_counter()
                prepared_image_path = _crop_remote_image_if_possible(image_path)
                gemini_result = process_page_with_gemini(
                    prepared_image_path,
                    lang=lang,
                    model_name=primary_model_name,
                    timeout_seconds=primary_timeout,
                    retries=primary_retries,
                )
                gemini_ms += int((time.perf_counter() - gemini_start) * 1000)
                rows = list(gemini_result.get("rows", []))
                if _absence_guard_enabled() and _should_run_visual_absence_guard(rows):
                    rows = _apply_visual_absence_guard(prepared_image_path, rows, lang)
                if _legacy_guard_enabled():
                    rows = _apply_legacy_signature_guard(prepared_image_path, source_path, page_number, rows, lang)
                if _presence_guard_enabled():
                    rows = _apply_visual_presence_guard(prepared_image_path, rows, lang)
                rows = _trim_rows_to_expected_count(rows, expected_name_count)

            if _overflow_legacy_fallback_enabled() and _rows_far_above_expected(rows, expected_name_count):
                legacy_page = _build_page_result_from_legacy(source_path, image_path, page_number, lang)
                if legacy_page is not None:
                    gemini_named = _named_row_count(rows)
                    legacy_named = _named_row_count(legacy_page.rows)
                    gemini_delta = abs(gemini_named - expected_name_count)
                    legacy_delta = abs(legacy_named - expected_name_count)
                    minimum_legacy_rows = max(1, expected_name_count // 2)
                    if legacy_delta + 1 < gemini_delta and legacy_named >= minimum_legacy_rows:
                        legacy_page.timings_ms["gemini_ms"] = gemini_ms
                        return legacy_page

            header = _normalize_header_dict(dict(gemini_result.get("header", {})))
            if str(gemini_result.get("model_used", "")).strip() == strong_model_name:
                can_escalate_to_strong = False
            if can_escalate_to_strong and _needs_strong_model_escalation(
                header,
                rows,
                profile,
                expected_name_count=expected_name_count,
            ):
                strong_start = time.perf_counter()
                strong_result = process_page_with_gemini(
                    prepared_image_path,
                    lang=lang,
                    model_name=strong_model_name,
                    timeout_seconds=_strong_model_timeout_seconds(),
                    retries=_strong_model_retries(),
                )
                gemini_ms += int((time.perf_counter() - strong_start) * 1000)
                strong_rows = list(strong_result.get("rows", []))
                if _absence_guard_enabled() and _should_run_visual_absence_guard(strong_rows):
                    strong_rows = _apply_visual_absence_guard(prepared_image_path, strong_rows, lang)
                if _legacy_guard_enabled():
                    strong_rows = _apply_legacy_signature_guard(prepared_image_path, source_path, page_number, strong_rows, lang)
                if _presence_guard_enabled():
                    strong_rows = _apply_visual_presence_guard(prepared_image_path, strong_rows, lang)
                strong_rows = _trim_rows_to_expected_count(strong_rows, expected_name_count)
                strong_header = _normalize_header_dict(dict(strong_result.get("header", {})))

                if _gemini_result_score(strong_header, strong_rows) >= _gemini_result_score(header, rows):
                    rows = strong_rows
                    header = strong_header
                    processor_used = "gemini_strong"
                else:
                    processor_used = "gemini_fast"
            else:
                processor_used = "gemini"

            partial_gemini_result = AttendancePageResult(
                page_number=page_number,
                rows=rows,
                header=header,
                processor_used=processor_used,
                timings_ms={"gemini_ms": gemini_ms},
            )
            if _can_accept_gemini_result(rows, profile, allow_low_row_count=False, expected_name_count=expected_name_count):
                return partial_gemini_result
            should_retry_hq = (
                source_path.suffix.lower() == ".pdf"
                and _high_quality_retry_enabled()
                and _should_retry_gemini_with_high_quality(
                header,
                rows,
                profile,
                expected_name_count=expected_name_count,
            ))
            if should_retry_hq:
                refined_result = _refine_result_with_high_quality(
                    source_path,
                    AttendancePageResult(
                        page_number=page_number,
                        rows=rows,
                        header=header,
                        processor_used="gemini",
                        timings_ms={"gemini_ms": gemini_ms},
                    ),
                    lang,
                )
                if _can_accept_gemini_result(
                    refined_result.rows,
                    profile,
                    allow_low_row_count=True,
                    expected_name_count=expected_name_count,
                ):
                    return refined_result
                partial_gemini_result = refined_result
            elif _can_accept_gemini_result(
                rows,
                profile,
                allow_low_row_count=True,
                expected_name_count=expected_name_count,
            ):
                return partial_gemini_result
        except Exception as exc:
            if prefetched_gemini_result is None:
                gemini_ms = int((time.perf_counter() - gemini_start) * 1000)
            gemini_error = exc

    if _use_documentai():
        docai_available, docai_reason = _documentai_fallback_available()
        if not docai_available:
            if partial_gemini_result is not None:
                _log(
                    "[HybridOCR] documentai_fallback_disabled "
                    f"page={page_number} reason={docai_reason}"
                )
                return partial_gemini_result
            if gemini_error is not None:
                raise gemini_error
            raise RuntimeError(f"Document AI indisponivel: {docai_reason}")
        fallback_start = time.perf_counter()
        try:
            docai_rows = process_page_with_documentai(image_path, lang=lang, page_number=page_number)
            fallback_ms = int((time.perf_counter() - fallback_start) * 1000)
            result = _build_page_result_from_documentai(page_number, docai_rows, fallback_ms)
            if gemini_ms:
                result.timings_ms["gemini_ms"] = gemini_ms
            return result
        except Exception as fallback_exc:
            if partial_gemini_result is not None:
                _log(
                    "[HybridOCR] documentai_fallback_unavailable "
                    f"page={page_number} error={fallback_exc}"
                )
                return partial_gemini_result
            if gemini_error is not None:
                raise gemini_error
            raise

    if partial_gemini_result is not None:
        return partial_gemini_result
    if gemini_error is not None:
        raise gemini_error
    raise RuntimeError(f"Nenhum processador remoto disponivel para a pagina {page_number}.")


def finalize_page_results(source_path: Path, page_results: list[AttendancePageResult]) -> list[ExtractedRow]:
    last_header = {"modulo": "", "curso": "", "turma": "", "data": ""}
    flattened: list[ExtractedRow] = []
    row_counter = 0

    # Extrai datas do nome do arquivo como referencia para validacao
    filename_dates = _extract_dates_from_filename(source_path.name)

    for page_result in sorted(page_results, key=lambda item: item.page_number):
        page_header = _merge_headers(last_header, _normalize_header_dict(page_result.header))
        
        # Valida a data extraida contra o nome do arquivo
        if page_header.get("data") and filename_dates:
            page_header["data"] = _validate_date_against_filename(
                page_header["data"], filename_dates
            )
        
        if any(page_header.values()):
            last_header = page_header

        for logical_row in page_result.rows:
            nome = str(logical_row.get("nome", "")).strip()
            if not nome:
                continue

            row_header = _normalize_header_dict({
                "modulo": str(logical_row.get("modulo") or page_header["modulo"]),
                "curso": str(logical_row.get("curso") or page_header["curso"]),
                "turma": str(logical_row.get("turma") or page_header["turma"]),
                "data": str(logical_row.get("data") or page_header["data"]),
            })

            for periodo, prefix in (
                ("Matutino", "matutino"),
                ("Vespertino", "vespertino"),
                ("Noturno", "noturno"),
            ):
                presenca, tipo, texto = _normalize_period_output(
                    str(logical_row.get(f"{prefix}_status", "Ausente")),
                    str(logical_row.get(f"{prefix}_texto", "")),
                    str(logical_row.get(f"{prefix}_tipo", "")),
                )
                row_counter += 1
                flattened.append(ExtractedRow(
                    source=str(source_path),
                    page=page_result.page_number,
                    row_number=row_counter,
                    columns=[
                        row_header["modulo"],
                        row_header["curso"],
                        row_header["turma"],
                        row_header["data"],
                        nome,
                        periodo,
                        presenca,
                        tipo,
                        texto,
                    ],
                ))

    return flattened


def _deduplicate_names(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """
    Unifica variantes do mesmo nome entre paginas.
    Ex: 'Alcicle Fernandes Peixoto' e 'Alciele Fernandes Peixoto' -> usa o mais frequente.
    """
    from difflib import SequenceMatcher

    name_counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for row in rows:
        if len(row.columns) <= 4:
            continue
        name = str(row.columns[4] or "").strip()
        if not name:
            continue
        if name not in first_seen:
            first_seen[name] = len(first_seen)
        name_counts[name] = name_counts.get(name, 0) + 1

    if len(name_counts) <= 1:
        return rows

    def _norm(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value)
        no_accents = "".join(char for char in decomposed if not unicodedata.combining(char)).lower().strip()
        return re.sub(r"\s+", " ", no_accents)

    def _token_set(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", value) if len(token) >= 3}

    normalized_names = {name: _norm(name) for name in name_counts}
    token_sets = {name: _token_set(normalized_names[name]) for name in name_counts}

    def _similar_enough(primary: str, candidate: str) -> bool:
        primary_norm = normalized_names[primary]
        candidate_norm = normalized_names[candidate]
        if not primary_norm or not candidate_norm:
            return False
        if primary_norm == candidate_norm:
            return True

        # Evita juntar entidades numeradas distintas (ex.: "Aluno 23" vs "Aluno 24").
        if any(char.isdigit() for char in primary_norm + candidate_norm):
            return False

        similarity = SequenceMatcher(None, primary_norm, candidate_norm).ratio()
        if similarity >= 0.90:
            return True

        # Captura typos comuns de OCR em nomes longos.
        if similarity >= 0.83 and min(len(primary_norm), len(candidate_norm)) >= 8:
            primary_tokens = token_sets[primary]
            candidate_tokens = token_sets[candidate]
            if primary_tokens and candidate_tokens:
                overlap = len(primary_tokens & candidate_tokens)
                if overlap >= min(len(primary_tokens), len(candidate_tokens)) - 1:
                    return True

        return False

    sorted_names = sorted(
        name_counts.keys(),
        key=lambda name: (-name_counts[name], first_seen.get(name, 0), -len(name)),
    )
    canonical_map: dict[str, str] = {}
    used_as_alias: set[str] = set()

    for canonical in sorted_names:
        if canonical in used_as_alias:
            continue
        for candidate in sorted_names:
            if candidate == canonical or candidate in used_as_alias:
                continue
            if not _similar_enough(canonical, candidate):
                continue
            canonical_map[candidate] = canonical
            used_as_alias.add(candidate)

    if not canonical_map:
        return rows

    for row in rows:
        if len(row.columns) <= 4:
            continue
        current = str(row.columns[4] or "").strip()
        if current in canonical_map:
            row.columns[4] = canonical_map[current]

    return rows


def _extract_dates_from_filename(filename: str) -> list[str]:
    """Extrai datas do nome do arquivo PDF (ex: '28 01 2022' -> '28/01/2022')."""
    # Padroes comuns: "28 01 2022", "28-01-2022", "28_01_2022"
    matches = re.findall(r'(\d{1,2})\s*[-_\s]\s*(\d{1,2})\s*[-_\s]\s*(\d{4})', filename)
    dates = []
    for day, month, year in matches:
        dates.append(f"{day.zfill(2)}/{month.zfill(2)}/{year}")
    return dates


def _validate_date_against_filename(extracted_date: str, filename_dates: list[str]) -> str:
    """
    Valida a data extraida pelo Gemini contra as datas no nome do arquivo.
    Se a data extraida tem o mesmo mes/ano que uma data do filename mas dia diferente,
    pode ser um erro de leitura. Nesse caso, mantemos a data extraida (pode ser pagina 2/3).
    Se o mes/ano nao bate com NENHUMA data do filename, corrige para a mais proxima.
    """
    if not filename_dates or not extracted_date:
        return extracted_date
    
    # Parse da data extraida
    date_match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', extracted_date)
    if not date_match:
        return extracted_date
    
    ext_day, ext_month, ext_year = date_match.groups()
    
    # Verifica se mes/ano bate com alguma data do filename
    for fn_date in filename_dates:
        fn_match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', fn_date)
        if not fn_match:
            continue
        fn_day, fn_month, fn_year = fn_match.groups()
        
        # Mesmo mes e ano = data valida (pode ser dia diferente em PDF multi-dia)
        if ext_month == fn_month and ext_year == fn_year:
            return extracted_date
    
    # Mes/ano nao bate - provavelmente erro de leitura
    # Usa a primeira data do filename como base, mantendo o dia extraido se razoavel
    fn_match = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', filename_dates[0])
    if fn_match:
        fn_day, fn_month, fn_year = fn_match.groups()
        # Se o dia extraido e razoavel (1-31), usa com mes/ano do filename
        if 1 <= int(ext_day) <= 31:
            return f"{ext_day.zfill(2)}/{fn_month}/{fn_year}"
        return filename_dates[0]
    
    return extracted_date


def _log(message: str) -> None:
    if _timing_logs_enabled():
        print(message)


def process_attendance_list(file_path: Path, output_path: Path, lang: str = "pt") -> int:
    total_start = time.perf_counter()

    if not _use_gemini() and not _use_documentai():
        from assinatura_lista import analyze_attendance_files

        return analyze_attendance_files([file_path], output_path, lang=lang)

    file_size_bytes = file_path.stat().st_size
    page_count, mime_type = inspect_pdf_document(file_path)
    profile = select_processing_profile(file_path, page_count=page_count, file_size_bytes=file_size_bytes)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 2)

    _log(
        "[HybridOCR] start "
        f"page_count={page_count} file_size_mb={file_size_mb} mime_type={mime_type} "
        f"profile_name={profile.name} stable_mode={_stable_production_mode_enabled()}"
    )
    if _use_gemini() and _gemini_warmup_enabled():
        try:
            warmup_info = maybe_warmup_gemini_runtime(timeout_seconds=_gemini_warmup_timeout_seconds())
            _log(
                "[HybridOCR] gemini_warmup "
                f"token_ready={warmup_info.get('token_ready')} "
                f"fast_model_available={warmup_info.get('fast_model_available')} "
                f"warmup_ms={warmup_info.get('elapsed_ms')} "
                f"cached={warmup_info.get('cached')} "
                f"error={warmup_info.get('error') or 'none'}"
            )
        except Exception as warmup_exc:
            _log(f"[HybridOCR] gemini_warmup_failed error={warmup_exc}")
    if _use_documentai():
        docai_available, docai_reason = _documentai_fallback_available()
        _log(
            "[HybridOCR] documentai_status "
            f"enabled=true available={docai_available} reason={docai_reason or 'ok'}"
        )

    render_start = time.perf_counter()
    if file_path.suffix.lower() == ".pdf":
        with tempfile.TemporaryDirectory(prefix="attendance_pipeline_") as temp_dir:
            render_dir = Path(temp_dir)
            image_paths = render_pdf_for_profile(file_path, render_dir, profile)
            render_ms = int((time.perf_counter() - render_start) * 1000)
            page_results = _process_rendered_pages(file_path, image_paths, profile, lang)
    else:
        image_paths = [file_path]
        render_ms = int((time.perf_counter() - render_start) * 1000)
        page_results = _process_rendered_pages(file_path, image_paths, profile, lang)

    rows = finalize_page_results(file_path, page_results)
    if not rows:
        from assinatura_lista import analyze_attendance_files

        return analyze_attendance_files([file_path], output_path, lang=lang)

    try:
        from postprocess_manuscrito import postprocess_extracted_rows

        rows = postprocess_extracted_rows(rows)
    except Exception as exc:
        _log(f"[HybridOCR] postprocess_skipped error={exc}")

    rows = _deduplicate_names(rows)

    from assinatura_lista import ATTENDANCE_HEADERS

    write_output(rows, output_path, ATTENDANCE_HEADERS)

    total_ms = int((time.perf_counter() - total_start) * 1000)
    processor_per_page = {result.page_number: result.processor_used for result in page_results}
    gemini_per_page = {result.page_number: result.timings_ms.get("gemini_ms", 0) for result in page_results}
    fallback_per_page = {result.page_number: result.timings_ms.get("fallback_ms", 0) for result in page_results}

    _log(
        "[HybridOCR] done "
        f"render_ms={render_ms} total_ms={total_ms} rows={len(rows)} "
        f"processor_used_per_page={processor_per_page} "
        f"gemini_ms_per_page={gemini_per_page} "
        f"fallback_ms_per_page={fallback_per_page}"
    )

    return len(rows)


def _process_rendered_pages(
    source_path: Path,
    image_paths: list[Path],
    profile: ProcessingProfile,
    lang: str,
) -> list[AttendancePageResult]:
    if len(image_paths) == 1:
        return [_process_single_page(source_path, 1, image_paths[0], profile, lang)]

    results: list[AttendancePageResult] = []

    if _force_page_by_page():
        max_workers = max(1, min(profile.max_concurrency, len(image_paths)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_process_single_page, source_path, page_number, image_path, profile, lang): page_number
                for page_number, image_path in enumerate(image_paths, start=1)
            }
            for future in as_completed(future_map):
                results.append(future.result())
    else:
        def process_block(start_page: int, block_paths: list[Path]) -> list[AttendancePageResult]:
            if len(block_paths) == 1:
                return [_process_single_page(source_path, start_page, block_paths[0], profile, lang)]

            expected_counts = [_estimate_expected_name_count(path) for path in block_paths]
            prepared_paths = [_crop_remote_image_if_possible(path) for path in block_paths]
            try:
                batch_start = time.perf_counter()
                batch_model = _fast_model_name() if _fast_model_first_pass_enabled() else _strong_model_name()
                batch_timeout = _fast_model_timeout_seconds() if _fast_model_first_pass_enabled() else _strong_model_timeout_seconds()
                batch_retries = _fast_model_retries() if _fast_model_first_pass_enabled() else _strong_model_retries()
                batch_results = process_pages_with_gemini(
                    prepared_paths,
                    lang=lang,
                    model_name=batch_model,
                    timeout_seconds=batch_timeout,
                    retries=batch_retries,
                )
                batch_ms = int((time.perf_counter() - batch_start) * 1000)
                per_page_ms = max(1, batch_ms // max(1, len(block_paths)))
                results_for_block: list[AttendancePageResult] = []
                for offset, image_path in enumerate(block_paths):
                    page_number = start_page + offset
                    prefetched = batch_results[offset] if offset < len(batch_results) else {"header": {}, "rows": []}
                    results_for_block.append(
                        _process_single_page(
                            source_path,
                            page_number,
                            image_path,
                            profile,
                            lang,
                            prefetched_gemini_result=prefetched,
                            prefetched_gemini_ms=per_page_ms,
                            expected_name_count_override=expected_counts[offset],
                        )
                    )
                return results_for_block
            except Exception:
                return [
                    _process_single_page(source_path, start_page + offset, image_path, profile, lang)
                    for offset, image_path in enumerate(block_paths)
                ]

        blocks: list[tuple[int, list[Path]]] = []
        batch_size = _batch_size_for_profile(profile, len(image_paths))
        index = 0
        while index < len(image_paths):
            start_page = index + 1
            block = image_paths[index:index + batch_size]
            blocks.append((start_page, block))
            index += len(block)

        max_workers = max(1, min(profile.max_concurrency, len(blocks)))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(process_block, start_page, block_paths): start_page
                for start_page, block_paths in blocks
            }
            for future in as_completed(future_map):
                results.extend(future.result())

    if source_path.suffix.lower() == ".pdf" and _refine_previous_page_enabled():
        sparse_pages = [
            result.page_number
            for result in results
            if _page_named_row_count(result) <= 2 and result.processor_used.startswith("gemini")
        ]
        result_by_page = {result.page_number: result for result in results}
        pages_to_refine = {
            page_number - 1
            for page_number in sparse_pages
            if page_number > 1
            and _page_named_row_count(result_by_page[page_number - 1]) <= _refine_prev_page_max_rows()
        }
        if pages_to_refine:
            refined_results: list[AttendancePageResult] = []
            for result in results:
                if result.page_number in pages_to_refine and result.processor_used.startswith("gemini"):
                    refined_results.append(_refine_result_with_high_quality(source_path, result, lang))
                else:
                    refined_results.append(result)
            results = refined_results

    if source_path.suffix.lower() == ".pdf" and _smart_refine_enabled():
        max_pages = _smart_refine_max_pages()
        if max_pages > 0:
            candidates = [
                result
                for result in results
                if _page_needs_smart_refine(result, profile)
            ]
            if candidates:
                ranked = sorted(
                    candidates,
                    key=lambda item: _smart_refine_priority(item, profile),
                    reverse=True,
                )
                selected_pages = {result.page_number for result in ranked[:max_pages]}
                max_workers = max(1, min(2, profile.max_concurrency, len(selected_pages)))
                selected_results = [result for result in results if result.page_number in selected_pages]
                refined_by_page: dict[int, AttendancePageResult] = {}
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(_refine_result_with_high_quality, source_path, result, lang): result.page_number
                        for result in selected_results
                    }
                    for future in as_completed(future_map):
                        refined = future.result()
                        refined_by_page[refined.page_number] = refined

                if refined_by_page:
                    updated_results: list[AttendancePageResult] = []
                    for result in results:
                        updated_results.append(refined_by_page.get(result.page_number, result))
                    results = updated_results

    return results

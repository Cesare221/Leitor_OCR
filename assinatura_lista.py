from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDOR_LOCAL_DIR = Path(__file__).resolve().parent / "vendor_local"
for candidate in (VENDOR_LOCAL_DIR, VENDOR_DIR):
    try:
        if (candidate / "PIL" / "Image.py").exists():
            sys.path.insert(0, str(candidate))
    except OSError:
        pass

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

from extrator_ocr import ExtractedRow, pdf_to_images, write_output, TESSERACT_CMD


_RAPIDOCR_LOCK = threading.Lock()
_RAPIDOCR_ENGINE = None
_RAPIDOCR_INIT_ATTEMPTED = False


def _rapidocr_enabled() -> bool:
    return os.environ.get("OCR_ENABLE_RAPIDOCR", "true").strip().lower() in {"1", "true", "yes", "sim", "on"}


def _rapidocr_min_confidence() -> float:
    raw = os.environ.get("OCR_RAPIDOCR_MIN_CONFIDENCE", "0.45").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.45


def _get_rapidocr_engine():
    global _RAPIDOCR_ENGINE, _RAPIDOCR_INIT_ATTEMPTED
    if not _rapidocr_enabled():
        return None
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE
    if _RAPIDOCR_INIT_ATTEMPTED:
        return None
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR_ENGINE is not None:
            return _RAPIDOCR_ENGINE
        if _RAPIDOCR_INIT_ATTEMPTED:
            return None
        _RAPIDOCR_INIT_ATTEMPTED = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPIDOCR_ENGINE = RapidOCR()
        except Exception:
            _RAPIDOCR_ENGINE = None
    return _RAPIDOCR_ENGINE


def _ocr_with_rapidocr(image: Image.Image) -> str:
    engine = _get_rapidocr_engine()
    if engine is None:
        return ""
    try:
        import numpy as np
        result, _ = engine(np.array(image.convert("RGB")))
    except Exception:
        return ""
    if not result:
        return ""

    min_conf = _rapidocr_min_confidence()
    tokens: list[str] = []
    for entry in result:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        text = str(entry[1]).strip()
        if not text:
            continue
        confidence = 0.0
        if len(entry) >= 3:
            try:
                confidence = float(entry[2])
            except (TypeError, ValueError):
                confidence = 0.0
        if confidence >= min_conf:
            tokens.append(text)
    return " ".join(tokens).strip()


def _use_documentai_fallback() -> bool:
    """Verifica se deve usar Document AI como fallback."""
    return os.environ.get("OCR_USE_DOCUMENTAI", "").strip().lower() in {"1", "true", "yes", "sim", "on"}


def _process_with_documentai(file_path: Path, output: Path, lang: str) -> tuple[list[ExtractedRow], HeaderInfo] | None:
    """Tenta processar com Document AI se disponível."""
    if not _use_documentai_fallback():
        return None
    
    try:
        from documentai_extractor import process_attendance_list_documentai
        
        # Processa com Document AI
        row_count = process_attendance_list_documentai(file_path, output, lang)
        
        # Se conseguiu processar, lê o output
        if row_count > 0 and output.exists():
            # Para simplificar, usa Tesseract para ler o output e criar rows
            # Em produção, você deveria parsear o Document AI diretamente
            pass
    except Exception as e:
        print(f"Document AI fallback failed: {e}")
    
    return None


SIGNATURE_COLUMNS = ("matutino", "vespertino", "noturno")
ATTENDANCE_HEADERS = [
    "Lista de Presença - Módulo",
    "Curso de Formação em",
    "Turma",
    "Data",
    "Nome Digitalizado",
    "Período",
    "Assinatura (Presente/Ausente)",
    "Tipo de Marca",
]


@dataclass
class LineGroup:
    start: int
    end: int

    @property
    def center(self) -> int:
        return (self.start + self.end) // 2


@dataclass
class TableGrid:
    horizontal: list[int]
    vertical: list[int]


@dataclass
class CellInk:
    signed: bool
    kind: str
    text: str
    confidence: str
    ink_pixels: int
    bbox_width: int
    bbox_height: int


@dataclass
class HeaderInfo:
    instituicao: str = ""
    nome_curso: str = ""
    modulo: str = ""
    turma: str = ""
    data: str = ""

    def values(self) -> list[str]:
        return [self.instituicao, self.nome_curso, self.modulo, self.turma, self.data]


def analyze_attendance_files(
    files: list[Path],
    output: Path,
    lang: str = "por+eng",
) -> int:
    rows: list[ExtractedRow] = []
    with tempfile.TemporaryDirectory(prefix="assinatura_lista_") as temp:
        work_dir = Path(temp)
        for file_path in files:
            image_paths = pdf_to_images(file_path, work_dir, 300) if file_path.suffix.lower() == ".pdf" else [file_path]
            last_good_header: HeaderInfo | None = None
            for page_number, image_path in enumerate(image_paths, start=1):
                page_rows, header = analyze_attendance_image(image_path, file_path, page_number, lang, last_good_header)
                if header and (header.nome_curso or header.modulo):
                    last_good_header = header
                rows.extend(page_rows)
    # Normaliza nomes duplicados (variantes curtas -> versao mais longa)
    rows = _normalize_names(rows)
    # Pós-processamento de texto manuscrito
    try:
        from postprocess_manuscrito import postprocess_extracted_rows
        rows = postprocess_extracted_rows(rows)
    except ImportError:
        pass
    write_output(rows, output, ATTENDANCE_HEADERS)
    return len(rows)


def _auto_rotate(image: Image.Image) -> Image.Image:
    """Detecta se a imagem esta rotacionada (retrato com tabela vertical) e corrige."""
    w, h = image.size
    if h <= w * 1.2:
        return image
    
    # Tenta ambas rotacoes e escolhe a que produz OCR legivel
    candidates = []
    for angle in (90, -90):
        rotated = image.rotate(angle, expand=True)
        grid = detect_table_grid(rotated)
        if grid:
            candidates.append((angle, rotated, grid))
    
    if not candidates:
        return image
    if len(candidates) == 1:
        return candidates[0][1]
    
    # Ambas rotacoes tem grid - testa OCR na coluna de nomes para decidir
    for angle, rotated, grid in candidates:
        real_v = _filter_real_columns(grid.vertical)
        row_intervals = data_row_intervals(grid.horizontal)
        if len(real_v) >= 3 and len(row_intervals) >= 3:
            col_widths = [(real_v[i+1] - real_v[i], i) for i in range(len(real_v)-1)]
            col_widths.sort(reverse=True)
            name_col = (real_v[col_widths[0][1]], real_v[col_widths[0][1] + 1])
            # Testa OCR em uma linha do meio
            mid = len(row_intervals) // 2
            top, bottom = row_intervals[mid]
            crop = rotated.crop((name_col[0]+8, top+3, name_col[1]-8, bottom-3))
            text = optional_ocr(crop, "por+eng", psm="7").strip()
            # Se tem texto com letras, esta e a rotacao certa
            if sum(1 for c in text if c.isalpha()) >= 5:
                return rotated
    
    # Fallback: retorna a primeira que tem grid
    return candidates[0][1]


def analyze_attendance_image(image_path: Path, source: Path, page_number: int, lang: str, fallback_header: HeaderInfo | None = None) -> tuple[list[ExtractedRow], HeaderInfo]:
    if Image is None or ImageOps is None:
        raise RuntimeError(
            "O modo de lista de presenca precisa da biblioteca Pillow. "
            "Instale com: pip install pillow"
    )
    image = Image.open(image_path).convert("RGB")
    image = _auto_rotate(image)
    grid = detect_table_grid(image)
    header = extract_header_info(image, grid, lang)
    # Se header desta pagina esta vazio, usa o fallback da pagina anterior
    if fallback_header and not header.nome_curso and not header.modulo:
        header = fallback_header
    elif fallback_header:
        # Preenche campos vazios com fallback
        if not header.modulo:
            header.modulo = fallback_header.modulo
        if not header.nome_curso:
            header.nome_curso = fallback_header.nome_curso
        if not header.turma:
            header.turma = fallback_header.turma
        if not header.data:
            header.data = fallback_header.data
        if not header.instituicao:
            header.instituicao = fallback_header.instituicao

    if not grid:
        return ([
            ExtractedRow(
                str(source),
                page_number,
                1,
                [header.modulo, header.nome_curso, header.turma, header.data,
                 "", "", "Ausente", "nao_detectado", ""],
            )
        ], header)

    row_intervals = data_row_intervals(grid.horizontal)
    if not row_intervals:
        return ([
            ExtractedRow(
                str(source),
                page_number,
                1,
                [header.modulo, header.nome_curso, header.turma, header.data,
                 "", "", "Ausente", "nao_detectado", ""],
            )
        ], header)

    vertical = grid.vertical
    real_v = _filter_real_columns(vertical)
    # Layout: Nº | Nome | Matutino | Vespertino | Noturno
    # A coluna de nomes é a segunda (entre real_v[1] e real_v[2]) - a mais larga
    if len(real_v) >= 3:
        col_widths = [(real_v[i+1] - real_v[i], i) for i in range(min(3, len(real_v)-1))]
        col_widths.sort(reverse=True)
        widest_idx = col_widths[0][1]
        name_col = (real_v[widest_idx], real_v[widest_idx + 1])
    elif len(real_v) >= 2:
        name_col = (real_v[0], real_v[1])
    else:
        name_col = (0, image.width // 2)
    signature_cols = signature_column_intervals(vertical, image.width)
    # Detecta nomes das colunas de assinatura (podem ser datas ou periodos)
    col_names = _detect_column_names(image, grid, signature_cols, row_intervals, lang)
    extracted: list[ExtractedRow] = []

    for row_index, (top, bottom) in enumerate(row_intervals, start=1):
        # Margem generosa na direita para evitar que OCR leia a borda da tabela
        name_crop = image.crop((
            max(0, name_col[0] + 8),
            max(0, top + 3),
            min(image.width, name_col[1] - 30),
            min(image.height, bottom - 3),
        ))
        printed_name = ocr_printed_name(name_crop, lang)
        # Remove ruído OCR: mantém apenas palavras que parecem nomes
        printed_name = _clean_name(printed_name)
        # Pula linhas sem nome (linhas vazias da tabela)
        if not printed_name:
            continue
        for column_name, (left, right) in zip(col_names, signature_cols):
            cell = crop_with_margin(image, left, top, right, bottom, 4)
            ink = analyze_cell_ink(cell, lang)
            # Se coluna e uma data, usa como campo Data; senao usa como Periodo
            is_date_col = bool(re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", column_name))
            row_data = header.data if not is_date_col else column_name
            row_period = column_name.capitalize() if not is_date_col else ""
            extracted.append(
                ExtractedRow(
                    str(source),
                    page_number,
                    row_index,
                    [
                        header.modulo,
                        header.nome_curso,
                        header.turma,
                        row_data,
                        printed_name,
                        row_period,
                        "Presente" if ink.signed else "Ausente",
                        ink.kind,
                        ink.text if ink.signed else "",
                    ],
                )
            )
    return (extracted, header)


def extract_header_info(image: Image.Image, grid: TableGrid | None, lang: str) -> HeaderInfo:
    if grid and grid.horizontal:
        # Encontra onde comecam os dados (primeira sequencia de linhas com altura <= 100px)
        row_intervals = data_row_intervals(grid.horizontal)
        if row_intervals:
            data_start = row_intervals[0][0]
        else:
            data_start = grid.horizontal[0]
        bottom = max(80, min(image.height, data_start))
    else:
        bottom = max(80, int(image.height * 0.28))
    crop = image.crop((0, 0, image.width, bottom))
    text = optional_ocr(crop, lang, psm="6")
    
    # Tambem le as primeiras 2 linhas da tabela (podem conter data e periodos)
    if grid and len(grid.horizontal) >= 3:
        row_intervals = data_row_intervals(grid.horizontal)
        if row_intervals:
            table_header_bottom = min(image.height, row_intervals[0][0] + (row_intervals[0][1] - row_intervals[0][0]) * 2)
        else:
            table_header_bottom = min(image.height, grid.horizontal[2])
        if table_header_bottom > bottom:
            table_header_crop = image.crop((0, bottom, image.width, table_header_bottom))
            table_text = optional_ocr(table_header_crop, lang, psm="6")
            text = text + "\n" + table_text
    
    return parse_header_text(text)


def parse_header_text(text: str) -> HeaderInfo:
    cleaned = clean_ocr_text(text)
    normalized = normalize_text(cleaned)

    info = HeaderInfo()

    # Data: dd/mm/aaaa ou dd-mm-aaaa
    date_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", normalized)
    if date_match:
        info.data = normalize_date(date_match.group(1))

    # Modulo: numero apos "MODULO" ou "MODUL" (OCR pode cortar)
    module_match = re.search(r"MODUL[O]?\s+(\d+|[IVXLCDM]+)\b", normalized)
    if not module_match:
        module_match = re.search(r"MODUL[O]?\s*\|?\s*(\d+|[IVXLCDM]+)\b(?!\s*CURSO)", normalized)
    if module_match:
        info.modulo = f"Modulo {module_match.group(1)}"
    elif re.search(r"MODUL", normalized):
        info.modulo = "Modulo"

    # Turma: numero/letra apos "TURMA"
    class_match = re.search(r"TURMA\s*(\d+[A-Z]?|[A-Z]\d*)\b", normalized)
    if class_match:
        info.turma = f"Turma {class_match.group(1)}"

    # Curso: texto apos "FORMACAO EM" ou "CURSO DE" ou "CURSO -" ate "Turma" ou fim
    course_match = re.search(
        r"FORMA[CÇ][AÃ]O\s+EM\s+(.+?)(?:\s+TURMA|\s+DATA|\s*$)", normalized
    )
    if course_match:
        info.nome_curso = title_keep_acronyms(course_match.group(1).strip())
    else:
        # Formato "CURSO - nome - TURMA"
        course_match = re.search(
            r"CURSO\s*[-–]\s*(.+?)(?:\s*[-–]\s*TURMA|\s+TURMA|\s*$)", normalized
        )
        if course_match:
            info.nome_curso = title_keep_acronyms(course_match.group(1).strip())
        else:
            course_match = re.search(
                r"CURSO\s+DE\s+(.+?)(?:\s+TURMA|\s+DATA|\s*$)", normalized
            )
            if course_match:
                raw_course = course_match.group(1).strip()
                raw_course = re.sub(r"^FORMA[CÇ][AÃ]O\s+EM\s+", "", raw_course)
                info.nome_curso = title_keep_acronyms(raw_course)

    # Instituicao: procura "INSTITUTO" ou nome longo sem palavras-chave de contexto
    inst_match = re.search(
        r"((?:RENASCER\s+SAUDE|INSTITUTO)[A-Z\s]+(?:HUMANO|SAUDE|EDUCACAO)(?:\s+[A-Z]+(?:\s+[A-Z]+)?)?)", normalized
    )
    if inst_match:
        raw_inst = inst_match.group(1).strip()
        # Corta antes de RUA, CONTATO, etc
        raw_inst = re.split(r"\s+(?:RUA|CONTATO|JARDIM|QD|LOTE)", raw_inst)[0].strip()
        # Remove palavras soltas de 1-3 chars no final (lixo OCR)
        raw_inst = re.sub(r"(\s+[A-Z]{1,3})+\s*$", "", raw_inst)
        info.instituicao = title_keep_acronyms(raw_inst)

    return info


def clean_ocr_text(text: str) -> str:
    lines = []
    for line in text.replace("|", " ").splitlines():
        compact = " ".join(line.split())
        if compact:
            lines.append(compact)
    return "\n".join(lines)


def normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_text = ascii_text.upper()
    ascii_text = ascii_text.replace("—", "-").replace("–", "-")
    return " ".join(ascii_text.split())


def normalize_date(value: str) -> str:
    parts = re.split(r"[/-]", value)
    if len(parts) != 3:
        return value
    day, month, year = parts
    if len(year) == 2:
        year = f"20{year}"
    return f"{day.zfill(2)}/{month.zfill(2)}/{year}"


def strip_after_date(text: str) -> str:
    return re.split(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text, maxsplit=1)[0].strip(" -:")


def strip_course_context(text: str) -> str:
    return re.split(r"\b[Tt][Uu][Rr][Mm][Aa]\s*[A-Za-z0-9-]+\b", text, maxsplit=1)[0].strip(" -:")


def title_keep_acronyms(text: str) -> str:
    lowercase_words = {"a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em", "para"}
    words = []
    for word in text.split():
        clean = word.strip()
        if normalize_text(clean).lower() in lowercase_words:
            words.append(clean.lower())
        elif clean.isupper() and len(clean) <= 4:
            words.append(clean)
        else:
            words.append(clean[:1].upper() + clean[1:].lower())
    return " ".join(words)


def detect_table_grid(image: Image.Image) -> TableGrid | None:
    gray = ImageOps.grayscale(image)
    width, height = gray.size
    pixels = gray.load()
    horizontal_scores: list[int] = []
    vertical_scores: list[int] = []

    for y in range(height):
        count = 0
        for x in range(width):
            if pixels[x, y] < 115:
                count += 1
        horizontal_scores.append(count)

    for x in range(width):
        count = 0
        for y in range(height):
            if pixels[x, y] < 115:
                count += 1
        vertical_scores.append(count)

    horizontal = group_lines(horizontal_scores, max(120, int(width * 0.32)), min_run=1)
    vertical = group_lines(vertical_scores, max(80, int(height * 0.20)), min_run=1)

    horizontal_centers = [group.center for group in horizontal]
    vertical_centers = [group.center for group in vertical]

    horizontal_centers = merge_close_positions(horizontal_centers, 5)
    vertical_centers = merge_close_positions(vertical_centers, 5)

    if len(horizontal_centers) < 8 or len(vertical_centers) < 3:
        return None

    return TableGrid(horizontal=horizontal_centers, vertical=vertical_centers)


def group_lines(scores: list[int], threshold: int, min_run: int) -> list[LineGroup]:
    groups: list[LineGroup] = []
    start: int | None = None
    for index, score in enumerate(scores):
        if score >= threshold and start is None:
            start = index
        elif score < threshold and start is not None:
            if index - start >= min_run:
                groups.append(LineGroup(start, index - 1))
            start = None
    if start is not None and len(scores) - start >= min_run:
        groups.append(LineGroup(start, len(scores) - 1))
    return groups


def merge_close_positions(values: list[int], distance: int) -> list[int]:
    if not values:
        return []
    merged = [values[0]]
    for value in values[1:]:
        if value - merged[-1] <= distance:
            merged[-1] = (merged[-1] + value) // 2
        else:
            merged.append(value)
    return merged


def data_row_intervals(horizontal: list[int]) -> list[tuple[int, int]]:
    intervals = [(horizontal[index], horizontal[index + 1]) for index in range(len(horizontal) - 1)]
    # Aceita linhas entre 12 e 80px (suporta DPI alto)
    small = [(top, bottom) for top, bottom in intervals if 12 <= bottom - top <= 100]
    if len(small) <= 2:
        return []

    # Agrupa intervalos consecutivos (sem gaps > 6px entre eles)
    best_group: list[tuple[int, int]] = []
    current: list[tuple[int, int]] = []
    last_bottom = -1
    for interval in small:
        top, bottom = interval
        if current and top - last_bottom > 6:
            if len(current) > len(best_group):
                best_group = current
            current = []
        current.append(interval)
        last_bottom = bottom
    if len(current) > len(best_group):
        best_group = current

    return best_group


def signature_column_intervals(vertical: list[int], width: int) -> list[tuple[int, int]]:
    # Filtra colunas falsas (bordas duplicadas) - largura mínima de 50px
    real = _filter_real_columns(vertical)
    # Layout: Nº | Nome | Matutino | Vespertino | Noturno
    # Identifica as 3 colunas de assinatura: larguras similares no lado direito
    if len(real) >= 5:
        # Procura 3 colunas consecutivas com larguras similares (variação < 30%)
        for start_idx in range(len(real) - 3):
            widths = [real[start_idx + i + 1] - real[start_idx + i] for i in range(3)]
            avg = sum(widths) / 3
            if avg > 0 and all(abs(w - avg) / avg < 0.3 for w in widths):
                return [(real[start_idx], real[start_idx+1]), (real[start_idx+1], real[start_idx+2]), (real[start_idx+2], real[start_idx+3])]
    if len(real) >= 4:
        return [(real[-3], real[-2]), (real[-2], real[-1]), (real[-1], width)]
    start = real[1] if len(real) > 1 else width // 2
    end = real[-1] if real else width
    step = max(1, (end - start) // 3)
    return [(start, start + step), (start + step, start + 2 * step), (start + 2 * step, end)]


def _detect_column_names(image: Image.Image, grid: TableGrid, signature_cols: list[tuple[int, int]], row_intervals: list[tuple[int, int]], lang: str) -> list[str]:
    """Detecta nomes das colunas de assinatura (datas ou periodos) lendo o header da tabela."""
    if not row_intervals:
        return list(SIGNATURE_COLUMNS)
    
    # Area do header da tabela: entre o topo da grid e o inicio dos dados
    data_start = row_intervals[0][0]
    header_lines = [h for h in grid.horizontal if h < data_start]
    if not header_lines:
        return list(SIGNATURE_COLUMNS)
    
    # Usa toda a area do header da tabela para cada coluna
    header_top = header_lines[0]
    header_bottom = data_start
    
    col_names = []
    for left, right in signature_cols:
        crop = image.crop((left + 4, header_top + 4, right - 4, header_bottom - 4))
        # Tenta OCR normal
        text = optional_ocr(crop, lang, psm="7").strip()
        date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
        if date_match:
            col_names.append(normalize_date(date_match.group(1)))
            continue
        # Tenta com rotacao (datas escritas verticalmente)
        for angle in (90, -90):
            rotated_crop = crop.rotate(angle, expand=True)
            text = optional_ocr(rotated_crop, lang, psm="7").strip()
            date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
            if date_match:
                col_names.append(normalize_date(date_match.group(1)))
                break
        else:
            # Verifica se texto contem periodo
            text_upper = text.upper()
            if "MATUTINO" in text_upper:
                col_names.append("Matutino")
            elif "VESPERTINO" in text_upper:
                col_names.append("Vespertino")
            elif "NOTURNO" in text_upper:
                col_names.append("Noturno")
            else:
                col_names.append("")
    
    # Se nenhuma coluna foi identificada, usa periodos padrao
    if not any(col_names):
        return list(SIGNATURE_COLUMNS)
    
    # Preenche vazios
    for i in range(len(col_names)):
        if not col_names[i]:
            col_names[i] = f"Coluna {i+1}"
    
    return col_names


def _normalize_names(rows: list[ExtractedRow]) -> list[ExtractedRow]:
    """Normaliza nomes duplicados: agrupa variantes do mesmo nome."""
    # Coleta todos os nomes unicos (indice 4 no columns)
    all_names: dict[str, int] = {}
    for r in rows:
        if len(r.columns) > 4 and r.columns[4]:
            all_names[r.columns[4]] = all_names.get(r.columns[4], 0) + 1
    
    def _strip_accents(s: str) -> str:
        decomposed = unicodedata.normalize("NFKD", s)
        return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    
    # Cria mapa: nome variante -> nome canonico (mais frequente ou mais longo)
    name_map: dict[str, str] = {}
    processed = set()
    sorted_names = sorted(all_names.keys(), key=lambda n: (-all_names[n], -len(n)))
    
    for canonical in sorted_names:
        if canonical in processed:
            continue
        canon_stripped = _strip_accents(canonical)
        for other in sorted_names:
            if other == canonical or other in processed:
                continue
            other_stripped = _strip_accents(other)
            # Mesmo nome sem acentos
            if canon_stripped == other_stripped:
                name_map[other] = canonical
                processed.add(other)
            # Um e prefixo do outro (com pelo menos 50% do tamanho)
            elif canon_stripped.startswith(other_stripped) and len(other_stripped) >= len(canon_stripped) * 0.5:
                name_map[other] = canonical
                processed.add(other)
            elif other_stripped.startswith(canon_stripped) and len(canon_stripped) >= len(other_stripped) * 0.5:
                name_map[canonical] = other
                processed.add(canonical)
                break
    
    if not name_map:
        return rows
    
    for r in rows:
        if len(r.columns) > 4 and r.columns[4] in name_map:
            r.columns[4] = name_map[r.columns[4]]
    return rows


def _clean_name(raw: str) -> str:
    """Remove ruido OCR de nomes extraidos, mantendo apenas palavras validas."""
    # Remove hifens colados que nao sao parte do nome
    raw = re.sub(r'-{2,}', ' ', raw)
    # Remove caracteres especiais isolados
    raw = re.sub(r'[|"\'`\[\]{}()\\+]', ' ', raw)
    # Separa palavras coladas por maiuscula (ex: "AbarecidaSartir" -> nao e nome valido)
    
    prepositions = {"da", "de", "do", "das", "dos", "e"}
    # Palavras que sao claramente lixo OCR
    noise_pattern = re.compile(r'^[A-Z]{1,4}$')  # Siglas curtas em maiuscula
    repeated_pattern = re.compile(r'^(.)\1+$', re.IGNORECASE)  # Letras repetidas
    # Palavras com maiusculas no meio (IJcho, EEE) - nao sao nomes
    mixed_caps = re.compile(r'^[A-Z][a-z]*[A-Z]')
    
    words = raw.split()
    cleaned = []
    for w in words:
        stripped = w.strip(".-,;:!?")
        if not stripped:
            continue
        letters = sum(1 for c in stripped if c.isalpha())
        total = len(stripped)
        
        # Rejeita palavras com hifen (lixo OCR)
        if '-' in stripped:
            break
            
        # Preposicoes validas se ja temos palavras antes
        if stripped.lower() in prepositions and cleaned:
            cleaned.append(stripped.lower())
            continue
        
        # Rejeita siglas curtas em maiuscula
        if noise_pattern.match(stripped) and stripped.lower() not in prepositions:
            break
        
        # Rejeita letras repetidas
        if repeated_pattern.match(stripped):
            break
        
        # Rejeita palavras com maiusculas no meio (IJcho, etc)
        if mixed_caps.match(stripped):
            break
        
        # Rejeita palavras muito curtas (< 3 letras) que nao sao preposicoes
        if letters < 3 and stripped.lower() not in prepositions:
            break
            
        # Palavra valida: maioria alfabetica, primeira letra maiuscula
        if letters >= 3 and letters / max(1, total) >= 0.8 and stripped[0].isupper():
            cleaned.append(stripped)
        elif cleaned:
            break
    
    # Remove preposicoes soltas no final
    while cleaned and cleaned[-1].lower() in prepositions:
        cleaned.pop()
    
    # Remove ultima palavra se for muito curta (< 4 letras) e nao e preposicao
    # Isso pega lixo OCR como "Ens", "Le", etc que passaram no filtro
    while cleaned and len(cleaned[-1]) < 4 and cleaned[-1].lower() not in prepositions:
        cleaned.pop()
    # Remove preposicoes que ficaram no final apos remocao
    while cleaned and cleaned[-1].lower() in prepositions:
        cleaned.pop()
    
    result = " ".join(cleaned)
    # Nome valido precisa ter pelo menos 5 caracteres
    if len(result) < 5:
        return ""
    return result


def _filter_real_columns(vertical: list[int]) -> list[int]:
    """Remove linhas verticais que formam colunas muito estreitas (< 50px)."""
    if len(vertical) <= 2:
        return vertical
    filtered = [vertical[0]]
    for v in vertical[1:]:
        if v - filtered[-1] >= 50:
            filtered.append(v)
    return filtered


def crop_with_margin(image: Image.Image, left: int, top: int, right: int, bottom: int, margin: int) -> Image.Image:
    return image.crop(
        (
            max(0, left + margin),
            max(0, top + margin),
            min(image.width, right - margin),
            min(image.height, bottom - margin),
        )
    )


def analyze_cell_ink(cell: Image.Image, lang: str, include_text: bool = True) -> CellInk:
    rgb = cell.convert("RGB")
    width, height = rgb.size
    ink_points: list[tuple[int, int]] = []
    for y in range(height):
        for x in range(width):
            r, g, b = rgb.getpixel((x, y))
            if is_handwriting_ink(r, g, b):
                ink_points.append((x, y))

    if not ink_points:
        return CellInk(False, "nao_assinado", "", "alta", 0, 0, 0)

    min_x = min(point[0] for point in ink_points)
    max_x = max(point[0] for point in ink_points)
    min_y = min(point[1] for point in ink_points)
    max_y = max(point[1] for point in ink_points)
    bbox_width = max_x - min_x + 1
    bbox_height = max_y - min_y + 1
    ink_pixels = len(ink_points)
    area = max(1, width * height)
    density = ink_pixels / area

    signed = ink_pixels >= 300 and bbox_width >= 40 and bbox_height >= 5 and density >= 0.01
    if not signed:
        return CellInk(False, "nao_assinado", "", "media", ink_pixels, bbox_width, bbox_height)

    kind = classify_signature(width, height, ink_pixels, bbox_width, bbox_height)
    text = optional_ocr(cell, lang, psm="13").strip() if include_text else ""
    confidence = "media" if text else "visual"
    return CellInk(True, kind, text, confidence, ink_pixels, bbox_width, bbox_height)


def is_handwriting_ink(r: int, g: int, b: int) -> bool:
    brightness = (r + g + b) / 3
    saturation = max(r, g, b) - min(r, g, b)
    blue_or_purple = b >= g + 8 and b >= r - 6 and saturation >= 24 and brightness < 235
    colored_pen = saturation >= 35 and brightness < 220 and b > 85
    return blue_or_purple or colored_pen


def classify_signature(width: int, height: int, ink_pixels: int, bbox_width: int, bbox_height: int) -> str:
    width_ratio = bbox_width / max(1, width)
    height_ratio = bbox_height / max(1, height)
    if width_ratio >= 0.42 and bbox_height >= 10 and ink_pixels >= 70:
        return "nome_manuscrito"
    if width_ratio >= 0.62 and height_ratio >= 0.35:
        return "nome_manuscrito"
    return "rubrica"


def ocr_printed_name(image: Image.Image, lang: str) -> str:
    """OCR otimizado para nomes impressos em tabela."""
    prepared = prepare_name_for_ocr(image)
    rapid_text = _ocr_with_rapidocr(prepared)
    if rapid_text and len(rapid_text) >= 4:
        return rapid_text

    if not TESSERACT_CMD:
        return ""
    with tempfile.TemporaryDirectory(prefix="ocr_name_") as temp:
        path = Path(temp) / "name.png"
        prepared.save(path)
        # PSM 7: linha unica de texto
        command = [TESSERACT_CMD, str(path), "stdout", "-l", lang, "--psm", "7"]
        result = subprocess.run(command, capture_output=True, timeout=30, encoding="utf-8", errors="replace")
        text = result.stdout.strip().strip("|").strip() if result.returncode == 0 else ""
        if text and len(text) >= 4:
            return text
        # Fallback: PSM 6 (bloco de texto)
        command[-1] = "6"
        result = subprocess.run(command, capture_output=True, timeout=30, encoding="utf-8", errors="replace")
        return result.stdout.strip().strip("|").strip() if result.returncode == 0 else ""


def optional_ocr(image: Image.Image, lang: str, psm: str) -> str:
    prepared = prepare_for_ocr(image)
    rapid_text = _ocr_with_rapidocr(prepared)
    if rapid_text:
        return " ".join(rapid_text.split())

    if not TESSERACT_CMD:
        return ""
    with tempfile.TemporaryDirectory(prefix="ocr_cell_") as temp:
        path = Path(temp) / "cell.png"
        prepared.save(path)
        command = [TESSERACT_CMD, str(path), "stdout", "-l", lang, "--psm", psm]
        result = subprocess.run(command, capture_output=True, timeout=30, encoding="utf-8", errors="replace")
        if result.returncode != 0 or not result.stdout:
            return ""
        return " ".join(result.stdout.split())


def prepare_for_ocr(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    scale = 3
    resized = gray.resize((gray.width * scale, gray.height * scale))
    return ImageOps.autocontrast(resized)


def prepare_name_for_ocr(image: Image.Image) -> Image.Image:
    """Pre-processamento otimizado para texto impresso (nomes na tabela)."""
    gray = ImageOps.grayscale(image)
    scale = 3
    resized = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)
    # Binarizacao com threshold para texto impresso preto
    threshold = 160
    binary = resized.point(lambda p: 255 if p > threshold else 0)
    return binary

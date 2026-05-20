from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import re
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

from extrator_ocr import ExtractedRow, pdf_to_images, write_output


SIGNATURE_COLUMNS = ("matutino", "vespertino", "noturno")
ATTENDANCE_HEADERS = [
    "instituicao",
    "nome_curso",
    "modulo",
    "turma",
    "data",
    "nome_impresso",
    "turno",
    "assinou",
    "tipo_marca",
    "texto_detectado",
    "confianca",
    "pixels_tinta",
    "largura_marca",
    "altura_marca",
    "observacao",
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
            for page_number, image_path in enumerate(image_paths, start=1):
                rows.extend(analyze_attendance_image(image_path, file_path, page_number, lang))
    write_output(rows, output, ATTENDANCE_HEADERS)
    return len(rows)


def analyze_attendance_image(image_path: Path, source: Path, page_number: int, lang: str) -> list[ExtractedRow]:
    if Image is None or ImageOps is None:
        raise RuntimeError(
            "O modo de lista de presenca precisa da biblioteca Pillow. "
            "Instale com: pip install pillow"
    )
    image = Image.open(image_path).convert("RGB")
    grid = detect_table_grid(image)
    header = extract_header_info(image, grid, lang)
    if not grid:
        return [
            ExtractedRow(
                str(source),
                page_number,
                1,
                header.values()
                + ["", "", "nao", "nao_detectado", "", "baixa", "0", "0", "0", "grade da tabela nao encontrada"],
            )
        ]

    row_intervals = data_row_intervals(grid.horizontal)
    if not row_intervals:
        return [
            ExtractedRow(
                str(source),
                page_number,
                1,
                header.values()
                + ["", "", "nao", "nao_detectado", "", "baixa", "0", "0", "0", "linhas de presenca nao encontradas"],
            )
        ]

    vertical = grid.vertical
    name_col = (vertical[0], vertical[1]) if len(vertical) >= 2 else (0, image.width // 2)
    signature_cols = signature_column_intervals(vertical, image.width)
    extracted: list[ExtractedRow] = []

    for row_index, (top, bottom) in enumerate(row_intervals, start=1):
        name_crop = crop_with_margin(image, name_col[0], top, name_col[1], bottom, 4)
        printed_name = optional_ocr(name_crop, lang, psm="7").strip()
        for column_name, (left, right) in zip(SIGNATURE_COLUMNS, signature_cols):
            cell = crop_with_margin(image, left, top, right, bottom, 4)
            ink = analyze_cell_ink(cell, lang)
            extracted.append(
                ExtractedRow(
                    str(source),
                    page_number,
                    row_index,
                    header.values()
                    + [
                        printed_name,
                        column_name,
                        "sim" if ink.signed else "nao",
                        ink.kind,
                        ink.text,
                        ink.confidence,
                        str(ink.ink_pixels),
                        str(ink.bbox_width),
                        str(ink.bbox_height),
                        "",
                    ],
                )
            )
    return extracted


def extract_header_info(image: Image.Image, grid: TableGrid | None, lang: str) -> HeaderInfo:
    if grid and grid.horizontal:
        bottom = max(80, min(image.height, grid.horizontal[0] - 2))
    else:
        bottom = max(80, int(image.height * 0.28))
    crop = image.crop((0, 0, image.width, bottom))
    text = optional_ocr(crop, lang, psm="6")
    return parse_header_text(text)


def parse_header_text(text: str) -> HeaderInfo:
    cleaned = clean_ocr_text(text)
    normalized = normalize_text(cleaned)
    lines = [line.strip(" -:") for line in cleaned.splitlines() if line.strip()]
    normalized_lines = [normalize_text(line) for line in lines]

    info = HeaderInfo()
    date_match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", normalized)
    if date_match:
        info.data = normalize_date(date_match.group(1))

    module_match = re.search(r"\bMODULO\s+([A-Z0-9IVXLCDM]+)\b", normalized)
    if module_match:
        info.modulo = f"Modulo {module_match.group(1)}"

    class_match = re.search(r"\bTURMA\s*([A-Z0-9-]+)\b", normalized)
    if class_match:
        info.turma = f"Turma {class_match.group(1)}"

    for original, line in zip(lines, normalized_lines):
        if "CURSO" in line and not info.nome_curso:
            info.nome_curso = title_keep_acronyms(strip_course_context(strip_after_date(original)))
        if "LISTA" in line and "MODULO" in line and not info.modulo:
            match = re.search(r"\bMODULO\s+([A-Z0-9IVXLCDM]+)\b", line)
            if match:
                info.modulo = f"Modulo {match.group(1)}"

    for original, line in zip(lines, normalized_lines):
        skip_words = ("RUA", "CONTATO", "LISTA", "CURSO", "MODULO", "TURMA")
        if any(word in line for word in skip_words):
            continue
        if len(line) >= 10 and sum(char.isalpha() for char in line) >= 8:
            info.instituicao = title_keep_acronyms(original)
            break

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
    small = [(top, bottom) for top, bottom in intervals if 12 <= bottom - top <= 42]
    if len(small) <= 4:
        return []

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

    if len(best_group) > 3:
        return best_group[3:] if len(best_group) >= 8 else best_group
    return best_group


def signature_column_intervals(vertical: list[int], width: int) -> list[tuple[int, int]]:
    if len(vertical) >= 5:
        return [(vertical[1], vertical[2]), (vertical[2], vertical[3]), (vertical[3], vertical[4])]
    if len(vertical) == 4:
        return [(vertical[1], vertical[2]), (vertical[2], vertical[3]), (vertical[2], vertical[3])]
    start = vertical[1] if len(vertical) > 1 else width // 2
    end = vertical[-1] if vertical else width
    step = max(1, (end - start) // 3)
    return [(start, start + step), (start + step, start + 2 * step), (start + 2 * step, end)]


def crop_with_margin(image: Image.Image, left: int, top: int, right: int, bottom: int, margin: int) -> Image.Image:
    return image.crop(
        (
            max(0, left + margin),
            max(0, top + margin),
            min(image.width, right - margin),
            min(image.height, bottom - margin),
        )
    )


def analyze_cell_ink(cell: Image.Image, lang: str) -> CellInk:
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

    signed = ink_pixels >= 18 and bbox_width >= 10 and bbox_height >= 3 and density >= 0.002
    if not signed:
        return CellInk(False, "nao_assinado", "", "media", ink_pixels, bbox_width, bbox_height)

    kind = classify_signature(width, height, ink_pixels, bbox_width, bbox_height)
    text = optional_ocr(cell, lang, psm="13").strip()
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


def optional_ocr(image: Image.Image, lang: str, psm: str) -> str:
    if not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory(prefix="ocr_cell_") as temp:
        path = Path(temp) / "cell.png"
        prepared = prepare_for_ocr(image)
        prepared.save(path)
        command = ["tesseract", str(path), "stdout", "-l", lang, "--psm", psm]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            return ""
        return " ".join(result.stdout.split())


def prepare_for_ocr(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    scale = 3
    resized = gray.resize((gray.width * scale, gray.height * scale))
    return ImageOps.autocontrast(resized)

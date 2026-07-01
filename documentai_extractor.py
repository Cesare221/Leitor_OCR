"""
documentai_extractor.py - Módulo de extração OCR usando Google Cloud Document AI.

Integração com Document AI para processamento de listas de presença com suporte a manuscritos.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPICallError
try:
    from google.cloud import documentai_v1 as documentai

    _DOCAI_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - depende do ambiente
    documentai = None  # type: ignore[assignment]
    _DOCAI_IMPORT_ERROR = exc


# Configurações do projeto
PROJECT_ID = os.environ.get("OCR_DOCUMENTAI_PROJECT_ID", "listreader")
LOCATION = os.environ.get("OCR_DOCUMENTAI_LOCATION", "us")
# Processor principal (Form Parser) - extrai estrutura de tabela
PROCESSOR_ID = os.environ.get("OCR_DOCUMENTAI_PROCESSOR_ID", "c50310d2f5b3f7a7")
# Processor OCR (Document OCR) - le manuscritos (opcional, vazio = desabilitado)
OCR_PROCESSOR_ID = os.environ.get("OCR_DOCUMENTAI_OCR_PROCESSOR_ID", "")

# Cache do cliente
_DOCUMENTAI_CLIENT: Optional[Any] = None


def is_documentai_runtime_ready() -> tuple[bool, str]:
    if documentai is None:
        return False, f"google-cloud-documentai indisponivel: {_DOCAI_IMPORT_ERROR}"
    if not PROJECT_ID or not LOCATION or not PROCESSOR_ID:
        return False, "variaveis OCR_DOCUMENTAI_* incompletas"
    return True, ""


def get_documentai_client() -> documentai.DocumentProcessorServiceClient:
    """Obtém ou cria o cliente do Document AI."""
    global _DOCUMENTAI_CLIENT
    ready, reason = is_documentai_runtime_ready()
    if not ready:
        raise RuntimeError(reason)
    if _DOCUMENTAI_CLIENT is None:
        api_endpoint = f"{LOCATION}-documentai.googleapis.com"
        client_options = ClientOptions(api_endpoint=api_endpoint)
        _DOCUMENTAI_CLIENT = documentai.DocumentProcessorServiceClient(
            client_options=client_options
        )
    return _DOCUMENTAI_CLIENT


def get_processor_name() -> str:
    """Obtém o nome completo do processor (Form Parser)."""
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{PROCESSOR_ID}"


def get_ocr_processor_name() -> str:
    """Obtém o nome completo do processor OCR (Document OCR)."""
    return f"projects/{PROJECT_ID}/locations/{LOCATION}/processors/{OCR_PROCESSOR_ID}"


def process_document_with_ocr(
    file_path: Path,
    mime_type: str = "application/pdf",
) -> Optional[documentai.Document]:
    """
    Processa documento usando Document OCR (otimizado para manuscritos).
    Retorna o documento processado ou None se falhar/não configurado.
    """
    # Desabilitado - o segundo processador causa timeout
    return None


def cell_has_overlap(cell_layout, ocr_layout, page_width: float, page_height: float) -> float:
    """
    Calcula sobreposicao entre uma celula e um elemento de texto OCR.
    Retorna proporcao da area do OCR layout dentro da celula (0.0 a 1.0).
    """
    if not cell_layout.bounding_poly or not ocr_layout.bounding_poly:
        return 0.0
    
    cell_verts = cell_layout.bounding_poly.normalized_vertices
    ocr_verts = ocr_layout.bounding_poly.normalized_vertices
    
    if not cell_verts or not ocr_verts:
        return 0.0
    
    cell_x_min = min(v.x for v in cell_verts)
    cell_x_max = max(v.x for v in cell_verts)
    cell_y_min = min(v.y for v in cell_verts)
    cell_y_max = max(v.y for v in cell_verts)
    
    ocr_x_min = min(v.x for v in ocr_verts)
    ocr_x_max = max(v.x for v in ocr_verts)
    ocr_y_min = min(v.y for v in ocr_verts)
    ocr_y_max = max(v.y for v in ocr_verts)
    
    x_overlap = max(0, min(cell_x_max, ocr_x_max) - max(cell_x_min, ocr_x_min))
    y_overlap = max(0, min(cell_y_max, ocr_y_max) - max(cell_y_min, ocr_y_min))
    intersection = x_overlap * y_overlap
    
    ocr_area = (ocr_x_max - ocr_x_min) * (ocr_y_max - ocr_y_min)
    if ocr_area <= 0:
        return 0.0
    
    return intersection / ocr_area


def get_handwriting_text_in_cell(
    cell_layout,
    ocr_document: Optional[documentai.Document],
    ocr_full_text: str,
    page_idx: int,
    overlap_threshold: float = 0.7,
) -> str:
    """
    Extrai texto manuscrito dentro da bounding box de uma celula.
    """
    if not ocr_document or page_idx >= len(ocr_document.pages):
        return ""
    
    ocr_page = ocr_document.pages[page_idx]
    cell_text_parts = []
    
    for paragraph in ocr_page.paragraphs:
        overlap = cell_has_overlap(cell_layout, paragraph.layout, ocr_page.dimension.width, ocr_page.dimension.height)
        if overlap >= overlap_threshold:
            text = get_text_from_layout(paragraph.layout, ocr_full_text)
            if text:
                cell_text_parts.append(text.strip())
    
    return " ".join(cell_text_parts).strip()


def process_document_with_documentai(
    file_path: Path,
    mime_type: str = "application/pdf",
) -> Optional[documentai.Document]:
    """
    Processa um documento usando Google Cloud Document AI (Form Parser).
    """
    try:
        client = get_documentai_client()
        processor_name = get_processor_name()
        
        with open(file_path, "rb") as file:
            content = file.read()
        
        raw_document = documentai.RawDocument(content=content, mime_type=mime_type)
        request = documentai.ProcessRequest(
            name=processor_name,
            raw_document=raw_document,
        )
        
        print(f"[DocumentAI] Processing with: {processor_name}")
        response = client.process_document(request=request, timeout=300)
        print(f"[DocumentAI] Done: {len(response.document.pages)} pages")
        return response.document
        
    except GoogleAPICallError as e:
        print(f"[DocumentAI] API error: {e}")
        raise
    except Exception as e:
        print(f"[DocumentAI] Error: {e}")
        raise


def get_text_from_layout(layout: documentai.Document.Page.Layout, full_text: str) -> str:
    """Extrai texto de um Layout do Document AI usando os text segments."""
    if not layout or not layout.text_anchor or not layout.text_anchor.text_segments:
        return ""
    
    segments = []
    for segment in layout.text_anchor.text_segments:
        start = int(segment.start_index) if segment.start_index else 0
        end = int(segment.end_index) if segment.end_index else 0
        segments.append(full_text[start:end])
    
    return "".join(segments).strip()


def extract_attendance_rows_from_documentai(
    file_path: Path,
    mime_type: str = "application/pdf",
) -> list[dict]:
    """
    Extrai linhas de uma lista de presença usando Document AI.
    """
    document = process_document_with_documentai(file_path, mime_type)
    if not document:
        return []
    
    full_text = document.text
    
    # OCR processor (opcional - só roda se configurado)
    ocr_document = process_document_with_ocr(file_path, mime_type)
    ocr_full_text = ocr_document.text if ocr_document else ""
    
    extracted_rows = []
    
    last_modulo = ""
    last_curso = ""
    last_turma = ""
    last_date = ""
    last_name_col_idx_global = -1
    last_period_cols_global: dict = {}
    
    for page_idx, page in enumerate(document.pages):
        page_number = page_idx + 1
        
        page_date = ""
        page_text_top = ""
        if page.layout:
            full_page_text = get_text_from_layout(page.layout, full_text)
            page_text_top = full_page_text[:500] if full_page_text else ""
        
        date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", page_text_top)
        if date_match:
            page_date = date_match.group(1)
        
        modulo = ""
        curso = ""
        turma = ""
        if "MÓDULO" in page_text_top.upper() or "MODULO" in page_text_top.upper():
            mod_match = re.search(r"M[ÓO]DULO\s*([IVX0-9]+)", page_text_top, re.IGNORECASE)
            if mod_match:
                modulo = f"Modulo {mod_match.group(1)}"
        curso_match = re.search(r"FORMA[ÇC][ÃA]O\s+EM\s+([^\n]+?)(?:\s+Turma|\n)", page_text_top, re.IGNORECASE)
        if curso_match:
            curso = curso_match.group(1).strip()
        turma_match = re.search(r"Turma\s+(\d+[A-Z]?)", page_text_top, re.IGNORECASE)
        if turma_match:
            turma = f"Turma {turma_match.group(1)}"
        
        if not modulo:
            modulo = last_modulo
        if not curso:
            curso = last_curso
        if not turma:
            turma = last_turma
        if not page_date:
            page_date = last_date
        
        if modulo:
            last_modulo = modulo
        if curso:
            last_curso = curso
        if turma:
            last_turma = turma
        if page_date:
            last_date = page_date
        
        last_name_col_idx = -1
        last_period_cols: dict = {}

        for table in page.tables:
            column_names = []
            if table.header_rows:
                for header_row in table.header_rows:
                    for cell in header_row.cells:
                        cell_text = get_text_from_layout(cell.layout, full_text).strip()
                        column_names.append(cell_text)
                    break
            
            name_col_idx = -1
            period_cols = {}
            
            for col_idx, col_name in enumerate(column_names):
                col_upper = col_name.upper()
                if "NOME" in col_upper:
                    name_col_idx = col_idx
                elif "MATUTINO" in col_upper:
                    period_cols[col_idx] = "Matutino"
                elif "VESPERTINO" in col_upper:
                    period_cols[col_idx] = "Vespertino"
                elif "NOTURNO" in col_upper:
                    period_cols[col_idx] = "Noturno"
            
            if name_col_idx < 0 and len(column_names) >= 2:
                name_col_idx = 1
            
            if not period_cols and len(column_names) >= 3:
                period_cols = {
                    len(column_names) - 3: "Matutino",
                    len(column_names) - 2: "Vespertino",
                    len(column_names) - 1: "Noturno",
                }
            
            if name_col_idx < 0 and last_name_col_idx >= 0:
                name_col_idx = last_name_col_idx
            if not period_cols and last_period_cols:
                period_cols = last_period_cols
            
            if name_col_idx >= 0:
                last_name_col_idx = name_col_idx
            if period_cols:
                last_period_cols = period_cols
            
            for body_row in table.body_rows:
                cells_text = []
                cells_layout = []
                for cell in body_row.cells:
                    cell_text = get_text_from_layout(cell.layout, full_text).strip()
                    cells_text.append(cell_text)
                    cells_layout.append(cell.layout)
                
                if name_col_idx >= len(cells_text):
                    continue
                
                nome = cells_text[name_col_idx].strip()
                nome = re.sub(r"^\d+\s*", "", nome)
                
                nome_parece_assinatura = (
                    not nome
                    or len(nome) < 3
                    or (len(nome) <= 6 and nome.isupper())
                    or (len(nome) <= 4)
                )
                
                if nome_parece_assinatura and name_col_idx < len(cells_layout):
                    ocr_name = get_handwriting_text_in_cell(
                        cells_layout[name_col_idx],
                        ocr_document,
                        ocr_full_text,
                        page_idx,
                        overlap_threshold=0.6,
                    )
                    ocr_name = re.sub(r"^\d+\s*", "", ocr_name).strip()
                    ocr_name = re.sub(r"^\d{1,2}\s+", "", ocr_name).strip()
                    if ocr_name and len(ocr_name) >= 5:
                        nome = ocr_name
                
                if not nome or len(nome) < 3:
                    has_any_signature = False
                    for col_idx in period_cols.keys():
                        if col_idx < len(cells_text) and cells_text[col_idx].strip() not in ("", "-", "--"):
                            has_any_signature = True
                            break
                    if not has_any_signature:
                        continue
                    nome = "(sem nome impresso)"
                
                for col_idx, periodo in period_cols.items():
                    if col_idx >= len(cells_text):
                        continue
                    
                    cell_content = cells_text[col_idx].strip()
                    
                    if col_idx < len(cells_layout) and ocr_document:
                        ocr_text = get_handwriting_text_in_cell(
                            cells_layout[col_idx],
                            ocr_document,
                            ocr_full_text,
                            page_idx,
                        )
                        if ocr_text and len(ocr_text) > len(cell_content):
                            cell_content = ocr_text
                    
                    is_signed = bool(cell_content) and cell_content not in ("-", "--", "—", "")
                    presenca = "Presente" if is_signed else "Ausente"
                    
                    if not is_signed:
                        tipo = "nao_assinado"
                    elif cell_content.lower() in ("sim", "ok", "x", "✓"):
                        tipo = "marcacao"
                    elif len(cell_content) > 10:
                        tipo = "nome_manuscrito"
                    else:
                        tipo = "rubrica"
                    
                    extracted_rows.append({
                        "page": page_number,
                        "modulo": modulo,
                        "curso": curso,
                        "turma": turma,
                        "data": page_date,
                        "nome": nome,
                        "periodo": periodo,
                        "presenca": presenca,
                        "tipo": tipo,
                        "manuscrito_texto": cell_content if is_signed else "",
                    })
    
    print(f"[DocumentAI] Extracted {len(extracted_rows)} rows total")
    return extracted_rows


def extract_attendance_page_documentai(
    file_path: Path,
    page_number: int = 1,
    lang: str = "pt",
) -> list:
    """Extrai uma pagina/imagem unica e devolve linhas no schema final."""
    del lang  # reservado para compatibilidade
    from extrator_ocr import ExtractedRow

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        mime_type = "application/pdf"
    elif suffix in (".jpg", ".jpeg"):
        mime_type = "image/jpeg"
    elif suffix == ".png":
        mime_type = "image/png"
    else:
        mime_type = "application/octet-stream"

    rows_data = extract_attendance_rows_from_documentai(file_path, mime_type)
    if not rows_data:
        return []

    try:
        from postprocess_manuscrito import postprocess_manuscrito_rows
        rows_data = postprocess_manuscrito_rows(rows_data)
    except Exception as e:
        print(f"[Postprocess] Skipped: {e}")

    extracted_rows = []
    for idx, row in enumerate(rows_data, start=1):
        extracted_rows.append(ExtractedRow(
            source=str(file_path),
            page=page_number,
            row_number=idx,
            columns=[
                row["modulo"],
                row["curso"],
                row["turma"],
                row["data"],
                row["nome"],
                row["periodo"],
                row["presenca"],
                row["tipo"],
                row.get("manuscrito_texto", ""),
            ],
        ))

    return extracted_rows



def process_attendance_list_documentai(
    file_path: Path,
    output_path: Path,
    lang: str = "pt",
) -> int:
    """
    Processa lista de presença usando Document AI e exporta para XLSX/CSV.
    """
    from extrator_ocr import write_output, ExtractedRow
    
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        mime_type = "application/pdf"
    elif suffix in (".jpg", ".jpeg"):
        mime_type = "image/jpeg"
    elif suffix == ".png":
        mime_type = "image/png"
    else:
        mime_type = "application/octet-stream"
    
    # Extrai dados via Document AI
    rows_data = extract_attendance_rows_from_documentai(file_path, mime_type)
    
    if not rows_data:
        raise RuntimeError("Document AI nao retornou dados utilizaveis.")
    
    # Pós-processamento (fail-safe: se der erro, continua sem)
    try:
        from postprocess_manuscrito import postprocess_manuscrito_rows
        rows_data = postprocess_manuscrito_rows(rows_data)
        print(f"[Postprocess] Applied successfully")
    except Exception as e:
        print(f"[Postprocess] Skipped: {e}")
    
    # Converte para ExtractedRow
    extracted_rows = []
    for idx, row in enumerate(rows_data, start=1):
        extracted_rows.append(ExtractedRow(
            source=str(file_path),
            page=row["page"],
            row_number=idx,
            columns=[
                row["modulo"],
                row["curso"],
                row["turma"],
                row["data"],
                row["nome"],
                row["periodo"],
                row["presenca"],
                row["tipo"],
                row.get("manuscrito_texto", ""),
            ],
        ))
    
    headers = [
        "Lista de Presença - Módulo",
        "Curso de Formação em",
        "Turma",
        "Data",
        "Nome Digitalizado",
        "Período",
        "Assinatura (Presente/Ausente)",
        "Tipo de Marca",
    ]
    
    write_output(extracted_rows, output_path, headers)
    return len(extracted_rows)

"""
gemini_ocr.py - Leitura de texto manuscrito usando Google Gemini Vision.

Usa o modelo Gemini para interpretar imagens de células com assinaturas/rubricas
e extrair o texto manuscrito com alta precisão.
"""
from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Optional

import vertexai
from vertexai.generative_models import GenerativeModel, Part, Image as VertexImage


# Configuração
PROJECT_ID = os.environ.get("OCR_DOCUMENTAI_PROJECT_ID", "listreader")
LOCATION = os.environ.get("OCR_GEMINI_LOCATION", "us-central1")
MODEL_NAME = os.environ.get("OCR_GEMINI_MODEL", "gemini-2.0-flash")

_INITIALIZED = False


def _ensure_init():
    """Inicializa o Vertex AI SDK."""
    global _INITIALIZED
    if not _INITIALIZED:
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        _INITIALIZED = True


def read_handwriting_from_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Usa Gemini Vision para ler texto manuscrito de uma imagem.
    
    Args:
        image_bytes: Bytes da imagem da célula
        mime_type: Tipo MIME da imagem
    
    Returns:
        Texto manuscrito lido, ou string vazia se não conseguir ler.
    """
    _ensure_init()
    
    try:
        model = GenerativeModel(MODEL_NAME)
        
        image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
        
        prompt = (
            "Olhe esta imagem de uma célula de uma lista de presença. "
            "Se houver texto manuscrito (assinatura, nome, rubrica, ou marcação como 'Sim', 'OK'), "
            "transcreva EXATAMENTE o que está escrito à mão. "
            "Se a célula estiver vazia ou tiver apenas um traço '--', responda VAZIO. "
            "Responda APENAS com o texto manuscrito, sem explicações."
        )
        
        response = model.generate_content(
            [image_part, prompt],
            generation_config={
                "max_output_tokens": 100,
                "temperature": 0.1,
            },
        )
        
        text = response.text.strip()
        
        # Se Gemini disse que está vazio
        if text.upper() in ("VAZIO", "VAZIA", "--", "-", "EMPTY", "NADA", ""):
            return ""
        
        return text
        
    except Exception as e:
        print(f"[Gemini] Error reading handwriting: {e}")
        return ""


def read_handwriting_batch(cells_data: list[tuple[bytes, str]]) -> list[str]:
    """
    Lê texto manuscrito de múltiplas células em batch.
    
    Args:
        cells_data: Lista de (image_bytes, mime_type)
    
    Returns:
        Lista de textos lidos (mesmo tamanho que input)
    """
    results = []
    for image_bytes, mime_type in cells_data:
        text = read_handwriting_from_image(image_bytes, mime_type)
        results.append(text)
    return results


def read_attendance_page(page_image_bytes: bytes, mime_type: str = "image/png") -> list[dict]:
    """
    Usa Gemini Vision para ler uma página inteira de lista de presença.
    Extrai nomes, períodos e status de presença diretamente da imagem.
    
    Args:
        page_image_bytes: Bytes da imagem da página completa
        mime_type: Tipo MIME da imagem
    
    Returns:
        Lista de dicts com: nome, matutino, vespertino, noturno
    """
    _ensure_init()
    
    try:
        model = GenerativeModel(MODEL_NAME)
        
        image_part = Part.from_data(data=page_image_bytes, mime_type=mime_type)
        
        prompt = """Analise esta imagem de uma lista de presença escolar/curso.

A tabela tem colunas: Número, Nome, Matutino, Vespertino, Noturno.

Para CADA linha da tabela, extraia:
1. O nome impresso (ou manuscrito se não houver impresso)
2. Para cada período (Matutino, Vespertino, Noturno), indique:
   - "Presente" se houver assinatura, rubrica, nome escrito, "Sim", "OK", ou qualquer marca manuscrita
   - "Ausente" se a célula estiver vazia ou tiver apenas "--"
3. Se houver texto manuscrito na célula de assinatura, transcreva-o

Responda em formato CSV com estas colunas (sem header):
nome|matutino_status|matutino_texto|vespertino_status|vespertino_texto|noturno_status|noturno_texto

Exemplo:
João da Silva|Presente|João Silva|Presente|JS|Ausente|
Maria Santos|Ausente||Presente|Sim|Presente|Maria

IMPORTANTE:
- Leia TODOS os nomes, incluindo os escritos à mão
- Se o nome está escrito à mão (não impresso), leia-o mesmo assim
- Transcreva o texto manuscrito o mais fielmente possível
- Inclua TODAS as linhas da tabela, não pule nenhuma"""

        response = model.generate_content(
            [image_part, prompt],
            generation_config={
                "max_output_tokens": 4096,
                "temperature": 0.1,
            },
        )
        
        text = response.text.strip()
        rows = []
        
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("nome|"):
                continue
            
            parts = line.split("|")
            if len(parts) < 7:
                continue
            
            rows.append({
                "nome": parts[0].strip(),
                "matutino_status": parts[1].strip(),
                "matutino_texto": parts[2].strip() if len(parts) > 2 else "",
                "vespertino_status": parts[3].strip() if len(parts) > 3 else "",
                "vespertino_texto": parts[4].strip() if len(parts) > 4 else "",
                "noturno_status": parts[5].strip() if len(parts) > 5 else "",
                "noturno_texto": parts[6].strip() if len(parts) > 6 else "",
            })
        
        return rows
        
    except Exception as e:
        print(f"[Gemini] Error reading attendance page: {e}")
        return []

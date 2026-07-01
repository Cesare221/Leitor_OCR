"""
postprocess_manuscrito.py - Pós-processamento de texto manuscrito extraído.

Aplica filtros de idioma, comparação com nome impresso e classificação
para melhorar a qualidade do texto manuscrito extraído pelo Document AI.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


# Padrões de caracteres não-latinos (cirílico, árabe, devanagari, etc.)
_NON_LATIN_PATTERN = re.compile(
    r'[\u0400-\u04FF'   # Cirílico
    r'\u0500-\u052F'    # Cirílico suplementar
    r'\u0600-\u06FF'    # Árabe
    r'\u0900-\u097F'    # Devanagari
    r'\u4E00-\u9FFF'    # CJK
    r'\u3040-\u309F'    # Hiragana
    r'\u30A0-\u30FF'    # Katakana
    r'\uAC00-\uD7AF'    # Hangul
    r']+'
)

# Palavras em inglês comuns que aparecem como lixo OCR em rubricas
_ENGLISH_NOISE_WORDS = {
    "the", "and", "for", "not", "you", "all", "can", "her", "was", "one",
    "our", "out", "are", "has", "his", "how", "its", "let", "may", "new",
    "now", "old", "see", "way", "who", "did", "get", "got", "him", "had",
    "just", "jest", "fox", "no", "one", "per",
    "also", "been", "call", "come", "each", "find", "from", "give",
    "have", "here", "high", "home", "keep", "last", "long", "look",
    "make", "many", "more", "most", "much", "must", "name", "next",
    "only", "over", "part", "some", "take", "tell", "than", "that",
    "them", "then", "they", "this", "time", "very", "when", "will",
    "with", "word", "work", "year", "harlots", "sorong",
}

# Preposições portuguesas que NÃO devem ser consideradas inglês
_PT_PREPOSITIONS = {"da", "de", "do", "das", "dos", "la", "em", "no", "na", "ao", "os", "as", "um"}


def _is_english_noise(word: str) -> bool:
    """Verifica se uma palavra é ruído em inglês (excluindo preposições PT)."""
    w = word.lower()
    return w in _ENGLISH_NOISE_WORDS and w not in _PT_PREPOSITIONS

# Marcações comuns de presença
_MARCACAO_PATTERNS = re.compile(
    r'^(sim|ok|x|✓|✔|presente|yes|s|v|visto|conf|confirmo|ok\s*[!.]?\s*sim)$',
    re.IGNORECASE,
)


def postprocess_manuscrito_rows(rows: list[dict]) -> list[dict]:
    """
    Aplica pós-processamento em todas as linhas extraídas.
    
    Cada row é um dict com pelo menos:
        - nome: nome impresso da pessoa
        - manuscrito_texto: texto manuscrito extraído
        - tipo: tipo de marca atual
        - presenca: "Presente" ou "Ausente"
    
    Retorna as mesmas linhas com campos corrigidos.
    """
    for row in rows:
        texto = row.get("manuscrito_texto", "")
        nome_impresso = row.get("nome", "")
        presenca = row.get("presenca", "Ausente")
        
        # Se ausente, não há texto para processar
        if presenca == "Ausente" or not texto:
            continue
        
        # Etapa 1: Filtro de idioma
        texto = _filtrar_idioma(texto)
        
        # Se ficou vazio após filtro, é rubrica ilegível
        if not texto or len(texto.strip()) < 2:
            row["manuscrito_texto"] = ""
            row["tipo"] = "rubrica"
            continue
        
        # Etapa 2: Detectar marcação simples
        if _is_marcacao(texto):
            row["manuscrito_texto"] = "Sim"
            row["tipo"] = "marcacao"
            continue
        
        # Etapa 3: Comparação com nome impresso
        texto, tipo = _comparar_com_nome(texto, nome_impresso)
        
        # Etapa 4: Limpeza final
        texto, tipo = _limpeza_final(texto, tipo, nome_impresso)
        
        row["manuscrito_texto"] = texto
        row["tipo"] = tipo
    
    return rows


def _filtrar_idioma(texto: str) -> str:
    """Remove caracteres não-latinos e limpa o texto."""
    # Remove caracteres não-latinos
    limpo = _NON_LATIN_PATTERN.sub("", texto)
    
    # Remove espaços extras
    limpo = " ".join(limpo.split())
    
    return limpo.strip()


def _is_marcacao(texto: str) -> bool:
    """Verifica se o texto é uma marcação simples de presença."""
    normalizado = texto.strip().lower()
    # Remove pontuação
    normalizado = re.sub(r'[!.,;:\s]+', ' ', normalizado).strip()
    
    if _MARCACAO_PATTERNS.match(normalizado):
        return True
    
    # "OK! Sim", "Ok Sim", etc.
    if re.match(r'^ok\s*[!.]?\s*sim$', normalizado, re.IGNORECASE):
        return True
    
    return False


def _comparar_com_nome(texto: str, nome_impresso: str) -> tuple[str, str]:
    """
    Compara texto manuscrito com o nome impresso.
    Retorna (texto_corrigido, tipo).
    """
    if not nome_impresso or nome_impresso == "(sem nome impresso)":
        # Sem nome para comparar — classifica pelo tamanho
        if len(texto) > 10:
            return texto, "nome_manuscrito"
        return texto, "rubrica"
    
    # Normaliza para comparação
    texto_norm = _normalizar_para_comparacao(texto)
    nome_norm = _normalizar_para_comparacao(nome_impresso)
    
    # Similaridade com nome completo
    sim_completo = _similaridade(texto_norm, nome_norm)
    
    # Similaridade com partes do nome (primeiro nome, sobrenome, etc.)
    partes_nome = nome_impresso.split()
    melhor_sim_parte = 0.0
    melhor_parte = ""
    
    for parte in partes_nome:
        if len(parte) < 3:  # Ignora preposições
            continue
        parte_norm = _normalizar_para_comparacao(parte)
        sim = _similaridade(texto_norm, parte_norm)
        if sim > melhor_sim_parte:
            melhor_sim_parte = sim
            melhor_parte = parte
    
    # Também compara cada palavra do texto com cada parte do nome
    palavras_texto = texto.split()
    match_parcial = False
    palavras_en_no_texto = sum(1 for p in palavras_texto if _is_english_noise(p))
    
    # Se mais da metade das palavras são inglês, não faz match parcial
    if palavras_en_no_texto < len(palavras_texto) * 0.5:
        for palavra in palavras_texto:
            if len(palavra) < 3:
                continue
            if _is_english_noise(palavra):
                continue
            palavra_norm = _normalizar_para_comparacao(palavra)
            for parte in partes_nome:
                if len(parte) < 4:  # Ignora partes curtas do nome
                    continue
                parte_norm = _normalizar_para_comparacao(parte)
                sim_palavra = _similaridade(palavra_norm, parte_norm)
                if sim_palavra >= 0.75:
                    match_parcial = True
                    break
                # Verifica se a parte do nome aparece como substring (palavras coladas)
                if len(parte_norm) >= 4 and parte_norm in palavra_norm:
                    match_parcial = True
                    break
                # Verifica substring parcial (pelo menos 70% dos chars da parte)
                if len(parte_norm) >= 5:
                    min_len = int(len(parte_norm) * 0.7)
                    for start in range(len(palavra_norm) - min_len + 1):
                        sub = palavra_norm[start:start + len(parte_norm)]
                        if _similaridade(sub, parte_norm) >= 0.75:
                            match_parcial = True
                            break
                if match_parcial:
                    break
            if match_parcial:
                break
    
    # Também compara com combinações (primeiro + último nome, etc.)
    if len(partes_nome) >= 2:
        primeiro_ultimo = f"{partes_nome[0]} {partes_nome[-1]}"
        sim_pl = _similaridade(texto_norm, _normalizar_para_comparacao(primeiro_ultimo))
        if sim_pl > sim_completo:
            sim_completo = sim_pl
    
    # Decisão baseada na similaridade
    # Se o texto contém palavras em inglês, exige similaridade mais alta
    has_english = any(_is_english_noise(p) for p in palavras_texto)
    threshold_completo = 0.55 if has_english else 0.35
    threshold_parte = 0.60 if has_english else 0.45
    
    if sim_completo >= 0.65:
        # Alta similaridade com nome completo — corrige para o nome
        return nome_impresso, "nome_manuscrito"
    elif melhor_sim_parte >= 0.70 and len(texto) >= 4 and not has_english:
        # Alta similaridade com parte do nome — é nome manuscrito
        return texto, "nome_manuscrito"
    elif match_parcial and len(texto) >= 4:
        # Pelo menos uma palavra do texto bate com parte do nome
        return texto, "nome_manuscrito"
    elif sim_completo >= threshold_completo or melhor_sim_parte >= threshold_parte:
        # Similaridade média — mantém texto original, é nome manuscrito
        return texto, "nome_manuscrito"
    else:
        # Baixa similaridade — é rubrica
        return texto, "rubrica"


def _limpeza_final(texto: str, tipo: str, nome_impresso: str) -> tuple[str, str]:
    """Limpeza final: remove lixo OCR óbvio."""
    # Se é rubrica, verifica se o texto é lixo em inglês
    if tipo == "rubrica":
        palavras = texto.lower().split()
        # Se todas as palavras são inglês comum, limpa
        if palavras and all(_is_english_noise(p) for p in palavras):
            return "", "rubrica"
        # Se tem apenas 1-2 caracteres sem sentido
        if len(texto.strip()) <= 2:
            return "", "rubrica"
    
    # Mesmo para nome_manuscrito, verifica se é lixo inglês sem relação com o nome
    if tipo == "nome_manuscrito" and nome_impresso:
        palavras = texto.lower().split()
        nome_norm = _normalizar_para_comparacao(nome_impresso)
        texto_norm = _normalizar_para_comparacao(texto)
        sim = _similaridade(texto_norm, nome_norm)
        
        # Se similaridade é muito baixa E as palavras parecem inglês, é rubrica
        if sim < 0.30:
            palavras_en = sum(1 for p in palavras if _is_english_noise(p))
            if palavras_en > 0 and palavras_en >= len(palavras) * 0.5:
                return "", "rubrica"
            # Texto muito curto sem relação com o nome
            if len(texto) <= 4:
                return "", "rubrica"
    
    # Se é rubrica com palavras em inglês, limpa o texto
    if tipo == "rubrica":
        palavras = texto.lower().split()
        palavras_en = sum(1 for p in palavras if _is_english_noise(p))
        if palavras_en > 0 and palavras_en >= len(palavras) * 0.4:
            return "", "rubrica"
    
    # Remove números soltos no início (resíduo de numeração de linha)
    texto = re.sub(r'^\d{1,3}\s+', '', texto)
    
    # Remove quebras de linha residuais
    texto = texto.replace('\n', ' ').strip()
    
    # Remove espaços duplos
    texto = ' '.join(texto.split())
    
    return texto, tipo


def _normalizar_para_comparacao(texto: str) -> str:
    """Normaliza texto para comparação de similaridade."""
    # Remove acentos
    decomposed = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Lowercase
    sem_acentos = sem_acentos.lower()
    # Remove pontuação e números
    sem_acentos = re.sub(r'[^a-z\s]', '', sem_acentos)
    # Remove espaços extras
    return ' '.join(sem_acentos.split())


def _similaridade(a: str, b: str) -> float:
    """Calcula similaridade entre duas strings (0.0 a 1.0)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def postprocess_extracted_rows(rows: list) -> list:
    """
    Versão que trabalha com ExtractedRow (formato usado no extrator).
    
    Espera rows com columns no formato:
    [modulo, curso, turma, data, nome, periodo, presenca, tipo, texto_manuscrito]
    """
    for row in rows:
        cols = row.columns if hasattr(row, 'columns') else row
        
        if len(cols) < 9:
            continue
        
        nome_impresso = cols[4]
        presenca = cols[6]
        tipo = cols[7]
        texto = cols[8] if len(cols) > 8 else ""
        
        # Se ausente, não processa
        if presenca == "Ausente" or not texto:
            continue
        
        # Etapa 1: Filtro de idioma
        texto = _filtrar_idioma(texto)
        
        if not texto or len(texto.strip()) < 2:
            cols[8] = ""
            cols[7] = "rubrica"
            continue
        
        # Etapa 2: Detectar marcação
        if _is_marcacao(texto):
            cols[8] = "Sim"
            cols[7] = "marcacao"
            continue
        
        # Etapa 3: Comparação com nome impresso
        texto, tipo = _comparar_com_nome(texto, nome_impresso)
        
        # Etapa 4: Limpeza final
        texto, tipo = _limpeza_final(texto, tipo, nome_impresso)
        
        cols[8] = texto
        cols[7] = tipo
    
    return rows

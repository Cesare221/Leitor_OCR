#!/usr/bin/env python3
"""
setup_documentai.py - Script de configuração do Google Cloud Document AI.

Este script ajuda a configurar o processor do Document AI no seu projeto.
"""
from __future__ import annotations

import os
import sys
import subprocess


def check_gcloud_installed() -> bool:
    """Verifica se o gcloud CLI está instalado."""
    try:
        result = subprocess.run(["gcloud", "--version"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_credentials() -> bool:
    """Verifica se as credenciais estão configuradas."""
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_project_id() -> str | None:
    """Obtém o project ID configurado."""
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.decode().strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def enable_api(project_id: str) -> bool:
    """Habilita a API do Document AI."""
    try:
        result = subprocess.run(
            [
                "gcloud", "services", "enable",
                "documentai.googleapis.com",
                f"--project={project_id}"
            ],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error enabling API: {e}")
        return False


def create_processor(project_id: str, location: str, processor_id: str) -> bool:
    """Cria um processor do Document AI."""
    try:
        result = subprocess.run(
            [
                "gcloud", "documentai", "processors", "create",
                f"--project={project_id}",
                f"--location={location}",
                f"--display-name={processor_id}",
                "--type=FORM_PARSER",
            ],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error creating processor: {e}")
        return False


def main() -> int:
    """Função principal."""
    print("=" * 60)
    print("Configuração do Google Cloud Document AI")
    print("=" * 60)
    
    # Verifica gcloud
    if not check_gcloud_installed():
        print("\n❌ O gcloud CLI não está instalado.")
        print("Instale em: https://cloud.google.com/sdk/docs/install")
        return 1
    
    print("\n✓ gcloud CLI encontrado")
    
    # Verifica credenciais
    if not check_credentials():
        print("\n⚠️  Credenciais não configuradas.")
        print("Execute: gcloud auth application-default login")
        print("\nOu configure variável de ambiente GOOGLE_APPLICATION_CREDENTIALS")
        print("apontando para seu arquivo de service account JSON.")
        return 1
    
    print("✓ Credenciais configuradas")
    
    # Obtém project ID
    project_id = get_project_id()
    if not project_id:
        project_id = os.environ.get("OCR_DOCUMENTAI_PROJECT_ID", "listreader")
    
    print(f"\nProject ID: {project_id}")
    
    # Pergunta se quer habilitar a API
    enable = input("\nHabilitar API do Document AI? (s/n): ").strip().lower()
    if enable in ("s", "sim", "y", "yes"):
        if enable_api(project_id):
            print("✓ API habilitada com sucesso")
        else:
            print("⚠️  Não foi possível habilitar a API automaticamente")
            print("Tente: gcloud services enable documentai.googleapis.com --project=" + project_id)
    
    # Pergunta se quer criar processor
    create = input("\nCriar novo processor? (s/n): ").strip().lower()
    if create in ("s", "sim", "y", "yes"):
        location = os.environ.get("OCR_DOCUMENTAI_LOCATION", "southamerica-east1")
        processor_id = os.environ.get("OCR_DOCUMENTAI_PROCESSOR_ID", "attendance-processor")
        
        if create_processor(project_id, location, processor_id):
            print(f"✓ Processor '{processor_id}' criado com sucesso")
        else:
            print("⚠️  Não foi possível criar o processor automaticamente")
            print("Tente criar manualmente no Console do GCP:")
            print(f"https://console.cloud.google.com/document-ai/processors?project={project_id}")
    
    print("\n" + "=" * 60)
    print("Configuração concluída!")
    print("=" * 60)
    print("\nPara usar, certifique-se de:")
    print("1. Estar autenticado: gcloud auth application-default login")
    print("2. Ter o processor criado no Console do GCP")
    print("3. Definir variáveis de ambiente (opcional):")
    print("   - OCR_DOCUMENTAI_PROJECT_ID")
    print("   - OCR_DOCUMENTAI_LOCATION")
    print("   - OCR_DOCUMENTAI_PROCESSOR_ID")
    print("   - OCR_USE_DOCUMENTAI=true")
    print("\nPara rodar com Document AI:")
    print("   OCR_USE_DOCUMENTAI=true python web_app.py")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

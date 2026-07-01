from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import threading
import time
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from attendance_pipeline import process_attendance_list
from assinatura_lista import analyze_attendance_files
from extrator_ocr import extract_rows, write_output, TESSERACT_CMD


def _use_documentai() -> bool:
    """Verifica se deve usar Document AI."""
    return os.environ.get("OCR_USE_DOCUMENTAI", "true").strip().lower() in {"1", "true", "yes", "sim", "on"}


def _use_gemini() -> bool:
    """Verifica se deve usar Gemini Vision."""
    return os.environ.get("OCR_USE_GEMINI", "true").strip().lower() in {"1", "true", "yes", "sim", "on"}


def _process_with_documentai(file_path: Path, output_path: Path, lang: str) -> int:
    """Processa com Document AI se disponível."""
    try:
        from documentai_extractor import process_attendance_list_documentai
        return process_attendance_list_documentai(file_path, output_path, lang)
    except Exception as e:
        print(f"Document AI failed: {e}")
        return 0


APP_NAME = "Leitor Seguro OCR"
BASE_DIR = Path(__file__).resolve().parent
ASSET_VERSION = os.environ.get("OCR_ASSET_VERSION", "20260529-1")
DATA_DIR = Path(os.environ.get("OCR_DATA_DIR", str(BASE_DIR / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "app.sqlite3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SESSION_TTL_SECONDS = 8 * 60 * 60
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_FIRESTORE_CLIENT: Any = None
_STORAGE_CLIENT: Any = None
JOB_WORKERS = max(1, int(os.environ.get("OCR_JOB_WORKERS", "3")))
_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="ocr-job")
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="18" fill="#0f766e"/>
<path d="M18 18h28v28H18z" fill="#ecfeff" opacity=".94"/>
<path d="M24 24h16M24 31h11M24 38h16" stroke="#0f766e" stroke-width="4" stroke-linecap="round"/>
<circle cx="45" cy="45" r="9" fill="#f59e0b"/>
<path d="M42 45l2 2 4-5" fill="none" stroke="#fff7ed" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>""".encode("utf-8")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def cloud_backend_enabled() -> bool:
    return os.environ.get("OCR_STORAGE_MODE", "local").strip().lower() in {"cloud", "gcp", "firestore"}


def firestore_client() -> Any:
    global _FIRESTORE_CLIENT
    if _FIRESTORE_CLIENT is None:
        from google.cloud import firestore

        _FIRESTORE_CLIENT = firestore.Client()
    return _FIRESTORE_CLIENT


def storage_client() -> Any:
    global _STORAGE_CLIENT
    if _STORAGE_CLIENT is None:
        from google.cloud import storage

        _STORAGE_CLIENT = storage.Client()
    return _STORAGE_CLIENT


def storage_bucket() -> Any:
    bucket_name = os.environ.get("OCR_GCS_BUCKET", "").strip()
    if not bucket_name:
        raise RuntimeError("Configure OCR_GCS_BUCKET para usar Cloud Run.")
    return storage_client().bucket(bucket_name)


def row_from_doc(doc: Any) -> dict[str, Any] | None:
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data


def session_cookie(token: str, max_age: int = SESSION_TTL_SECONDS) -> str:
    secure = "; Secure" if env_flag("OCR_COOKIE_SECURE") or os.environ.get("K_SERVICE") else ""
    return f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"


def setup_token_required() -> str:
    return os.environ.get("OCR_SETUP_TOKEN", "").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    if cloud_backend_enabled():
        return
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                lgpd_consent_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_file TEXT NOT NULL,
                output_file TEXT,
                output_format TEXT NOT NULL,
                mode TEXT NOT NULL,
                rows_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                retention_until TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                ip TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


def has_users() -> bool:
    if cloud_backend_enabled():
        docs = firestore_client().collection("users").limit(1).stream()
        return any(True for _ in docs)
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        return bool(row["total"])


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"pbkdf2_sha256$260000${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def audit(user_id: int | None, action: str, details: str = "", ip: str = "") -> None:
    if cloud_backend_enabled():
        firestore_client().collection("audit_log").add(
            {
                "user_id": str(user_id) if user_id is not None else None,
                "action": action,
                "details": details[:1000],
                "ip": ip,
                "created_at": utc_now(),
            }
        )
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, details, ip, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, action, details[:1000], ip, utc_now()),
        )


def create_session(user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if cloud_backend_enabled():
        firestore_client().collection("sessions").document(token_hash).set(
            {
                "user_id": str(user_id),
                "csrf_token": csrf,
                "expires_at": expires_at,
                "created_at": utc_now(),
            }
        )
        return token, csrf
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, csrf, expires_at, utc_now()),
        )
    return token, csrf


def delete_session(token: str) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if cloud_backend_enabled():
        firestore_client().collection("sessions").document(token_hash).delete()
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (token_hash,))


def parse_cookie(header: str | None) -> dict[str, str]:
    cookie = SimpleCookie()
    if header:
        cookie.load(header)
    return {key: morsel.value for key, morsel in cookie.items()}


def get_session(cookie_header: str | None) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str | None]:
    token = parse_cookie(cookie_header).get("session")
    if not token:
        return None, None, None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if cloud_backend_enabled():
        client = firestore_client()
        session = row_from_doc(client.collection("sessions").document(token_hash).get())
        if not session or session["expires_at"] < int(time.time()):
            client.collection("sessions").document(token_hash).delete()
            return None, None, None
        user = row_from_doc(client.collection("users").document(str(session["user_id"])).get())
        return user, session, token
    with db() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (token_hash,)).fetchone()
        if not session or session["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM sessions WHERE id = ?", (token_hash,))
            return None, None, None
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return user, session, token


def create_user(name: str, email: str, password: str) -> str | int:
    created_at = utc_now()
    if cloud_backend_enabled():
        doc = firestore_client().collection("users").document()
        doc.set(
            {
                "name": name,
                "email": email,
                "password_hash": hash_password(password),
                "role": "admin",
                "lgpd_consent_at": created_at,
                "created_at": created_at,
            }
        )
        return doc.id
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (name, email, password_hash, lgpd_consent_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, email, hash_password(password), created_at, created_at),
        )
        return int(cursor.lastrowid)


def get_user_by_email(email: str) -> Any:
    if cloud_backend_enabled():
        docs = firestore_client().collection("users").where("email", "==", email).limit(1).stream()
        for doc in docs:
            return row_from_doc(doc)
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def list_jobs_for_user(user_id: str | int, limit: int = 30) -> list[Any]:
    if cloud_backend_enabled():
        query = (
            firestore_client()
            .collection("jobs")
            .where("user_id", "==", str(user_id))
        )
        try:
            docs = query.order_by("created_at", direction="DESCENDING").limit(limit).stream()
            rows = []
            for doc in docs:
                row = row_from_doc(doc)
                if row:
                    rows.append(row)
            return rows
        except Exception as exc:
            print(f"[CloudStorage] jobs_query_index_fallback reason={exc}")
            docs = query.limit(max(limit * 4, limit)).stream()
            rows = []
            for doc in docs:
                row = row_from_doc(doc)
                if row:
                    rows.append(row)
            rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return rows[:limit]
    with db() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def _job_dict(job: Any) -> dict[str, Any]:
    if isinstance(job, dict):
        return dict(job)
    return dict(job)


def dashboard_summary(jobs: list[Any]) -> dict[str, int]:
    normalized = [_job_dict(job) for job in jobs]
    processing = sum(1 for job in normalized if job.get("status") == "processando")
    completed = sum(1 for job in normalized if job.get("status") == "concluido")
    failed = sum(1 for job in normalized if job.get("status") == "erro")
    rows_total = sum(int(job.get("rows_count") or 0) for job in normalized)
    return {
        "total": len(normalized),
        "processing": processing,
        "completed": completed,
        "failed": failed,
        "rows_total": rows_total,
    }


def render_jobs_table_rows(jobs: list[Any], csrf_token: str) -> str:
    rows: list[str] = []
    for raw_job in jobs:
        job = _job_dict(raw_job)
        status = escape(str(job.get("status", "")))
        status_class = {
            "concluido": "success",
            "erro": "danger",
            "processando": "warning",
        }.get(job.get("status", ""), "neutral")
        download = ""
        if job.get("status") == "concluido" and job.get("output_file"):
            download = f'<a class="button small" href="/download?id={job["id"]}">Baixar</a>'
        elif job.get("status") == "processando":
            download = '<span class="muted">Em andamento</span>'
        error = f'<span class="muted">{escape(str(job.get("error") or ""))}</span>' if job.get("error") else ""
        rows.append(
            f"""
            <tr>
              <td>
                <div class="job-name">{escape(str(job.get("original_name", "")))}</div>
                <div class="job-meta">ID {escape(str(job.get("id", "")))} · {escape(str(job.get("created_at", ""))[:19].replace("T", " "))}</div>
              </td>
              <td><span class="pill {status_class}">{status}</span></td>
              <td>{escape(str(job.get("rows_count") or 0))}</td>
              <td>{escape(str(job.get("output_format", "")).upper())}</td>
              <td>{escape(str(job.get("retention_until", ""))[:10])}</td>
              <td class="actions">
                {download}
                <form method="post" action="/delete-job">
                  <input type="hidden" name="csrf" value="{escape(csrf_token)}">
                  <input type="hidden" name="job_id" value="{escape(str(job.get("id", "")))}">
                  <button class="ghost danger small" type="submit">Excluir</button>
                </form>
              </td>
            </tr>
            {f'<tr><td colspan="6">{error}</td></tr>' if error else ''}
            """
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty">Nenhum processamento ainda.</td></tr>'


def create_job_record(
    user_id: str | int,
    original_name: str,
    stored_file: str,
    output_file: str,
    output_format: str,
    mode: str,
    retention_until: str,
) -> str | int:
    created_at = utc_now()
    if cloud_backend_enabled():
        doc = firestore_client().collection("jobs").document()
        doc.set(
            {
                "user_id": str(user_id),
                "original_name": original_name,
                "stored_file": stored_file,
                "output_file": output_file,
                "output_format": output_format,
                "mode": mode,
                "rows_count": 0,
                "status": "processando",
                "error": None,
                "retention_until": retention_until,
                "created_at": created_at,
            }
        )
        return doc.id
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (user_id, original_name, stored_file, output_file, output_format, mode, status, retention_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, original_name, stored_file, output_file, output_format, mode, "processando", retention_until, created_at),
        )
        return int(cursor.lastrowid)


def update_job_record(job_id: str | int, values: dict[str, Any]) -> None:
    if cloud_backend_enabled():
        firestore_client().collection("jobs").document(str(job_id)).update(values)
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [job_id]
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)


def start_job_processing(
    *,
    job_id: str | int,
    user_id: str | int,
    stored_path: Path,
    output_path: Path,
    stored_ref: str,
    output_ref: str,
    output_name: str,
    file_content_type: str,
    processor: str,
    mode: str,
    lang: str,
    original_name: str,
    client_ip: str,
    cleanup_dir: Path | None = None,
) -> None:
    def run() -> None:
        try:
            if processor == "attendance":
                row_count = process_attendance_list(stored_path, output_path, lang=lang)
            else:
                rows = extract_rows([stored_path], lang=lang, psm="6", dpi=300, mode=mode)
                write_output(rows, output_path)
                row_count = len(rows)
            if cloud_backend_enabled():
                upload_cloud_blob(stored_ref, stored_path, file_content_type or "application/octet-stream")
                upload_cloud_blob(output_ref, output_path, mimetypes.guess_type(output_name)[0] or "application/octet-stream")
            update_job_record(job_id, {"status": "concluido", "rows_count": row_count, "error": None})
            audit(user_id, "process_success", f"job={job_id}; arquivo={original_name}", client_ip)
        except BaseException as exc:
            message = str(exc) or exc.__class__.__name__
            update_job_record(job_id, {"status": "erro", "error": message[:1000]})
            audit(user_id, "process_error", f"job={job_id}; erro={message[:500]}", client_ip)
        finally:
            if cleanup_dir and cleanup_dir.exists():
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    _JOB_EXECUTOR.submit(run)


def get_job_for_user(job_id: str | int, user_id: str | int) -> Any:
    if cloud_backend_enabled():
        job = row_from_doc(firestore_client().collection("jobs").document(str(job_id)).get())
        if job and job.get("user_id") == str(user_id):
            return job
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()


def delete_job_record(job_id: str | int) -> None:
    if cloud_backend_enabled():
        firestore_client().collection("jobs").document(str(job_id)).delete()
        return
    with db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def list_audit_entries(limit: int = 100) -> list[dict[str, Any]]:
    if cloud_backend_enabled():
        users: dict[str, str] = {}
        entries = []
        docs = (
            firestore_client()
            .collection("audit_log")
            .order_by("created_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        for doc in docs:
            item = row_from_doc(doc)
            if not item:
                continue
            user_id = item.get("user_id")
            if user_id and user_id not in users:
                user = row_from_doc(firestore_client().collection("users").document(str(user_id)).get())
                users[str(user_id)] = user["email"] if user else ""
            item["email"] = users.get(str(user_id), "") if user_id else ""
            entries.append(item)
        return entries
    with db() as conn:
        return conn.execute(
            """
            SELECT audit_log.*, users.email
            FROM audit_log
            LEFT JOIN users ON users.id = audit_log.user_id
            ORDER BY audit_log.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def form_decode(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def parse_content_disposition(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in value.split(";"):
        item = item.strip()
        if "=" in item:
            key, raw = item.split("=", 1)
            result[key.lower()] = raw.strip().strip('"')
    return result


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if "boundary=" not in content_type:
        raise ValueError("Formulario multipart invalido.")
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
    form: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for raw_part in body.split(b"--" + boundary):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        header_blob, _, content = part.partition(b"\r\n\r\n")
        headers: dict[str, str] = {}
        for line in header_blob.decode("latin-1", errors="replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower()] = value.strip()
        disposition = parse_content_disposition(headers.get("content-disposition", ""))
        name = disposition.get("name", "")
        filename = Path(disposition.get("filename", "")).name
        if filename:
            files[name] = {
                "filename": filename,
                "content_type": headers.get("content-type", "application/octet-stream"),
                "content": content,
            }
        elif name:
            form[name] = content.decode("utf-8", errors="replace")
    return form, files


def html_page(title: str, body: str, user: sqlite3.Row | None = None) -> bytes:
    nav = ""
    if user:
        nav = f"""
        <nav class="topbar">
          <a class="brand" href="/dashboard">{APP_NAME}</a>
          <div class="nav-actions">
            <a href="/audit">Auditoria</a>
            <a href="/security">Seguranca</a>
            <a href="/privacy">LGPD</a>
            <form method="post" action="/logout">
              <button class="ghost" type="submit">Sair</button>
            </form>
          </div>
        </nav>
        """
    else:
        nav = f"""
        <nav class="topbar">
          <a class="brand" href="/">{APP_NAME}</a>
          <div class="nav-actions">
            <a href="/security">Seguranca</a>
            <a href="/privacy">LGPD</a>
          </div>
        </nav>
        """

    return f"""<!doctype html>
    <html lang="pt-BR">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{escape(title)} · {APP_NAME}</title>
      <link rel="icon" href="/favicon.ico" sizes="any">
      <link rel="stylesheet" href="/static/styles.css?v={ASSET_VERSION}">
      <script src="/static/app.js?v={ASSET_VERSION}" defer></script>
    </head>
    <body>
      {nav}
      <main>{body}</main>
    </body>
    </html>""".encode("utf-8")


def alert(message: str, kind: str = "info") -> str:
    return f'<div class="alert {kind}">{escape(message)}</div>' if message else ""


def login_page(message: str = "") -> bytes:
    body = f"""
    <section class="auth-shell">
      <div class="auth-panel">
        <p class="eyebrow">Acesso restrito</p>
        <h1>Entre para processar documentos</h1>
        {alert(message, "error" if message else "info")}
        <form class="form-grid" method="post" action="/login">
          <label>E-mail
            <input name="email" type="email" autocomplete="email" required>
          </label>
          <label>Senha
            <input name="password" type="password" autocomplete="current-password" required>
          </label>
          <button type="submit">Entrar</button>
        </form>
      </div>
      <aside class="assurance-panel">
        <h2>Controles ativos</h2>
        <ul>
          <li>Senha com hash forte PBKDF2</li>
          <li>Sessao expira automaticamente</li>
          <li>Cookie HttpOnly e SameSite</li>
          <li>Protecao CSRF em operacoes sensiveis</li>
          <li>Auditoria de login, upload, download e exclusao</li>
        </ul>
      </aside>
    </section>
    """
    return html_page("Login", body)


def setup_page(message: str = "", setup_token: str = "") -> bytes:
    token_field = f'<input type="hidden" name="setup_token" value="{escape(setup_token)}">' if setup_token else ""
    body = f"""
    <section class="auth-shell">
      <div class="auth-panel">
        <p class="eyebrow">Primeiro acesso</p>
        <h1>Crie o administrador</h1>
        {alert(message, "error" if message else "info")}
        <form class="form-grid" method="post" action="/setup">
          {token_field}
          <label>Nome
            <input name="name" required autocomplete="name">
          </label>
          <label>E-mail
            <input name="email" type="email" required autocomplete="email">
          </label>
          <label>Senha
            <input name="password" type="password" minlength="10" required autocomplete="new-password">
          </label>
          <label>Confirmar senha
            <input name="confirm" type="password" minlength="10" required autocomplete="new-password">
          </label>
          <label class="check">
            <input name="consent" type="checkbox" value="yes" required>
            <span>Li a politica de privacidade e autorizo o tratamento dos dados para extracao OCR.</span>
          </label>
          <button type="submit">Criar conta</button>
        </form>
      </div>
      <aside class="assurance-panel">
        <h2>Base LGPD</h2>
        <p>A aplicacao registra finalidade, consentimento, auditoria e prazo de retencao dos arquivos.</p>
      </aside>
    </section>
    """
    return html_page("Configurar", body)


def dashboard_page(user: sqlite3.Row, session: sqlite3.Row, message: str = "") -> bytes:
    jobs = list_jobs_for_user(user["id"], 30)
    rows = []
    for job in jobs:
        status = escape(job["status"])
        download = ""
        if job["status"] == "concluido" and job["output_file"]:
            download = f'<a class="button small" href="/download?id={job["id"]}">Baixar</a>'
        error = f'<span class="muted">{escape(job["error"] or "")}</span>' if job["error"] else ""
        rows.append(
            f"""
            <tr>
              <td>{escape(job["original_name"])}</td>
              <td><span class="pill">{status}</span></td>
              <td>{job["rows_count"]}</td>
              <td>{escape(job["output_format"].upper())}</td>
              <td>{escape(job["retention_until"][:10])}</td>
              <td class="actions">
                {download}
                <form method="post" action="/delete-job">
                  <input type="hidden" name="csrf" value="{escape(session["csrf_token"])}">
                  <input type="hidden" name="job_id" value="{job["id"]}">
                  <button class="ghost danger small" type="submit">Excluir</button>
                </form>
              </td>
            </tr>
            {f'<tr><td colspan="6">{error}</td></tr>' if error else ''}
            """
        )
    table = "".join(rows) or '<tr><td colspan="6" class="empty">Nenhum processamento ainda.</td></tr>'
    body = f"""
    <section class="dashboard-head">
      <h1>Lista de Presença - Extração OCR</h1>
    </section>

    {alert(message, "success" if "Concluido" in message else "error" if message else "info")}

    <section class="workspace">
      <form class="upload-panel" id="uploadForm" method="post" action="/process" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{escape(session["csrf_token"])}">
        <input type="hidden" name="processor" value="attendance">
        <input type="hidden" name="lang" value="por">
        <input type="hidden" name="mode" value="table">
        <input type="hidden" name="purpose" value="yes">

        <div class="file-upload">
          <label class="file-label" for="fileInput">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="17 8 12 3 7 8"/>
              <line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            <span>Escolher arquivo</span>
          </label>
          <input name="document" type="file" accept=".pdf,.jpg,.jpeg,.png" required id="fileInput">
          <p class="file-name" id="fileName">PDF, JPG ou PNG até 25 MB</p>
        </div>

        <div class="field-row">
          <label>Formato de saída
            <select name="format">
              <option value="xlsx">XLSX (Excel)</option>
              <option value="csv">CSV</option>
            </select>
          </label>
          <label>Retenção
            <select name="retention_days">
              <option value="7">7 dias</option>
              <option value="1">1 dia</option>
              <option value="30">30 dias</option>
            </select>
          </label>
        </div>

        <button type="submit" class="btn-process" id="btnProcess">Processar Lista</button>
      </form>
    </section>

    <section class="history">
      <h2>Processamentos recentes</h2>
      <table>
        <thead>
          <tr><th>Arquivo</th><th>Status</th><th>Linhas</th><th>Formato</th><th>Retenção</th><th>Ações</th></tr>
        </thead>
        <tbody>{table}</tbody>
      </table>
    </section>

    <script>
    document.getElementById('fileInput').addEventListener('change', function() {{
      var f = this.files[0];
      if (f) document.getElementById('fileName').textContent = f.name + ' (' + (f.size/1024/1024).toFixed(1) + ' MB)';
    }});

    var uploadForm = document.getElementById('uploadForm');
    var btnProcess = document.getElementById('btnProcess');

    uploadForm.addEventListener('submit', function() {{
      // Desabilita botão APÓS o submit iniciar (usando setTimeout)
      setTimeout(function() {{
        btnProcess.disabled = true;
        btnProcess.innerHTML = '<span class="spinner"></span> Processando...';
      }}, 100);

      // Timeout de segurança: se demorar mais de 15min, reabilita o botão
      setTimeout(function() {{
        btnProcess.disabled = false;
        btnProcess.innerHTML = 'Processar Lista';
      }}, 900000);
    }});
    </script>
    """
    return html_page("Dashboard", body, user)


def dashboard_page(user: sqlite3.Row, session: sqlite3.Row, message: str = "") -> bytes:
    jobs = [_job_dict(job) for job in list_jobs_for_user(user["id"], 30)]
    summary = dashboard_summary(jobs)
    table = render_jobs_table_rows(jobs, session["csrf_token"])
    processing_note = (
        "Ha processamentos em andamento. A lista abaixo atualiza automaticamente."
        if summary["processing"]
        else "Nenhum processamento em andamento no momento."
    )
    body = f"""
    <section class="dashboard-shell" id="dashboardApp" data-csrf="{escape(session["csrf_token"])}" data-feed-url="/jobs-feed" data-process-url="/process">
      <section class="hero-card">
        <div class="hero-copy">
          <p class="eyebrow">Operacao OCR de listas de presenca</p>
          <h1>Processamento continuo, leitura assistida e rastreabilidade em um unico painel.</h1>
          <p class="hero-text">Envie PDFs ou imagens, acompanhe o progresso em tempo real e baixe os resultados sem precisar recarregar a tela entre um documento e outro.</p>
        </div>
        <div class="hero-side">
          <div class="status-orb {'is-live' if summary['processing'] else ''}"></div>
          <strong id="queueHeadline">{escape(processing_note)}</strong>
          <p id="queueSubline">O painel faz polling automatico do status e reabilita o envio assim que o job entra na fila.</p>
        </div>
      </section>

      {alert(message, "success" if "Concluido" in message else "error" if message else "info")}
      <div class="alert info dashboard-inline-alert" id="liveAlert" hidden></div>

      <section class="metrics-grid" id="metricsGrid">
        <article class="metric-card">
          <span class="metric-label">Total de jobs</span>
          <strong class="metric-value" id="metricTotal">{summary["total"]}</strong>
          <span class="metric-help">Ultimos 30 processamentos</span>
        </article>
        <article class="metric-card accent">
          <span class="metric-label">Em andamento</span>
          <strong class="metric-value" id="metricProcessing">{summary["processing"]}</strong>
          <span class="metric-help">Atualizacao automatica ativa</span>
        </article>
        <article class="metric-card success">
          <span class="metric-label">Concluidos</span>
          <strong class="metric-value" id="metricCompleted">{summary["completed"]}</strong>
          <span class="metric-help">Resultados prontos para download</span>
        </article>
        <article class="metric-card danger">
          <span class="metric-label">Com erro</span>
          <strong class="metric-value" id="metricFailed">{summary["failed"]}</strong>
          <span class="metric-help">Itens que exigem revisao</span>
        </article>
      </section>

      <section class="workspace-grid">
        <form class="upload-panel elevated" id="uploadForm" method="post" action="/process" enctype="multipart/form-data">
          <input type="hidden" name="csrf" value="{escape(session["csrf_token"])}">
          <input type="hidden" name="processor" value="attendance">
          <input type="hidden" name="lang" value="por">
          <input type="hidden" name="mode" value="table">
          <input type="hidden" name="purpose" value="yes">

          <div class="panel-head">
            <div>
              <p class="eyebrow">Novo envio</p>
              <h2>Adicionar nova lista</h2>
            </div>
            <span class="panel-badge">Fila viva</span>
          </div>

          <div class="file-upload">
            <label class="file-label" for="fileInput">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="17 8 12 3 7 8"/>
                <line x1="12" y1="3" x2="12" y2="15"/>
              </svg>
              <span>Selecionar arquivo</span>
            </label>
            <input name="document" type="file" accept=".pdf,.jpg,.jpeg,.png" required id="fileInput">
            <p class="file-name" id="fileName">PDF, JPG ou PNG ate 25 MB</p>
          </div>

          <div class="field-row">
            <label>Formato de saida
              <select name="format">
                <option value="xlsx">XLSX (Excel)</option>
                <option value="csv">CSV</option>
              </select>
            </label>
            <label>Retencao
              <select name="retention_days">
                <option value="7">7 dias</option>
                <option value="1">1 dia</option>
                <option value="30">30 dias</option>
              </select>
            </label>
          </div>

          <button type="submit" class="btn-process" id="btnProcess">Enviar para processamento</button>
        </form>

        <aside class="insight-panel">
          <div class="panel-head">
            <div>
              <p class="eyebrow">Monitoramento</p>
              <h2>Estado da operacao</h2>
            </div>
          </div>
          <ul class="insight-list">
            <li><strong id="insightPrimary">{summary["processing"]} job(s)</strong> em processamento agora.</li>
            <li><strong id="insightRows">{summary["rows_total"]}</strong> linhas estruturadas nos jobs listados.</li>
            <li>Upload e acompanhamento funcionam sem recarregar a tela a cada novo arquivo.</li>
            <li>Downloads e exclusoes continuam disponiveis no historico abaixo.</li>
          </ul>
        </aside>
      </section>

      <section class="history glass-card">
        <div class="history-head">
          <div>
            <p class="eyebrow">Historico ativo</p>
            <h2>Processamentos recentes</h2>
          </div>
          <div class="history-tools">
            <span class="history-note" id="historyNote">{escape(processing_note)}</span>
            <form method="post" action="/clear-history" onsubmit="return confirm('Deseja apagar todo o historico de processamentos e arquivos gerados?');">
              <input type="hidden" name="csrf" value="{escape(session["csrf_token"])}">
              <button class="ghost danger small" type="submit">Limpar historico</button>
            </form>
          </div>
        </div>
        <div class="table-scroll">
          <table class="jobs-table">
            <thead>
              <tr><th>Arquivo</th><th>Status</th><th>Linhas</th><th>Formato</th><th>Retencao</th><th>Acoes</th></tr>
            </thead>
            <tbody id="jobsTableBody">{table}</tbody>
          </table>
        </div>
      </section>
    </section>
    """
    return html_page("Dashboard", body, user)


def privacy_page(user: sqlite3.Row | None = None) -> bytes:
    body = """
    <section class="document-page">
      <p class="eyebrow">LGPD</p>
      <h1>Politica de privacidade operacional</h1>
      <p>Esta aplicacao trata documentos enviados pelo usuario para a finalidade especifica de extrair texto e estruturar dados em CSV ou XLSX.</p>
      <h2>Dados tratados</h2>
      <p>Nome, e-mail, registros de acesso, arquivos enviados, resultados extraidos e eventos de auditoria.</p>
      <h2>Base legal</h2>
      <p>O operador deve definir a base legal aplicavel antes de processar documentos. A tela de upload exige confirmacao de finalidade e autorizacao operacional.</p>
      <h2>Retencao e exclusao</h2>
      <p>Cada processamento recebe prazo de retencao. O usuario autenticado pode excluir arquivos e resultados pela propria tela.</p>
      <h2>Direitos do titular</h2>
      <p>Solicitacoes de acesso, correcao, exclusao ou informacoes sobre tratamento devem ser direcionadas ao controlador responsavel pela operacao.</p>
      <h2>Aviso</h2>
      <p>Este texto e uma base tecnica. Para uso publico ou corporativo formal, revise com responsavel juridico e encarregado de dados.</p>
    </section>
    """
    return html_page("LGPD", body, user)


def security_page(user: sqlite3.Row | None = None) -> bytes:
    body = """
    <section class="document-page">
      <p class="eyebrow">Seguranca</p>
      <h1>Controles implementados</h1>
      <div class="control-grid">
        <article><h2>Autenticacao</h2><p>Senhas armazenadas com PBKDF2, salt unico e comparacao resistente a timing attack.</p></article>
        <article><h2>Sessao</h2><p>Tokens aleatorios, cookie HttpOnly, SameSite Strict e expiracao automatica.</p></article>
        <article><h2>CSRF</h2><p>Operacoes de processamento e exclusao exigem token por sessao.</p></article>
        <article><h2>Upload</h2><p>Extensoes permitidas, limite de 25 MB e nomes aleatorios no armazenamento.</p></article>
        <article><h2>Auditoria</h2><p>Eventos de setup, login, processamento, download e exclusao sao registrados.</p></article>
        <article><h2>Retencao</h2><p>Arquivos podem ser removidos pelo usuario e recebem prazo declarado.</p></article>
      </div>
      <p class="note">Para internet publica, use proxy HTTPS, backups criptografados, segregacao de ambiente, monitoramento e revisao de permissao por perfil.</p>
    </section>
    """
    return html_page("Seguranca", body, user)


def audit_page(user: sqlite3.Row) -> bytes:
    entries = list_audit_entries(100)
    rows = []
    for item in entries:
        rows.append(
            f"""
            <tr>
              <td>{escape(item["created_at"])}</td>
              <td>{escape(item["email"] or "-")}</td>
              <td><span class="pill">{escape(item["action"])}</span></td>
              <td>{escape(item["details"] or "")}</td>
              <td>{escape(item["ip"] or "")}</td>
            </tr>
            """
        )
    table = "".join(rows) or '<tr><td colspan="5" class="empty">Nenhum evento registrado.</td></tr>'
    body = f"""
    <section class="document-page">
      <p class="eyebrow">Rastreabilidade</p>
      <h1>Auditoria de seguranca</h1>
      <p>Ultimos 100 eventos de acesso, processamento, download, exclusao e limpeza de retencao.</p>
      <div class="table-scroll">
        <table class="audit-table">
          <thead>
            <tr><th>Data</th><th>Usuario</th><th>Evento</th><th>Detalhes</th><th>IP</th></tr>
          </thead>
          <tbody>{table}</tbody>
        </table>
      </div>
    </section>
    """
    return html_page("Auditoria", body, user)


def clean_old_sessions() -> None:
    if cloud_backend_enabled():
        docs = firestore_client().collection("sessions").where("expires_at", "<", int(time.time())).stream()
        for doc in docs:
            doc.reference.delete()
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def purge_expired_jobs() -> None:
    now = utc_now()
    if cloud_backend_enabled():
        docs = firestore_client().collection("jobs").where("retention_until", "<", now).stream()
        jobs = [row_from_doc(doc) for doc in docs]
        for job in jobs:
            if not job:
                continue
            delete_cloud_blob(job.get("stored_file"))
            delete_cloud_blob(job.get("output_file"))
            delete_job_record(job["id"])
            audit(job["user_id"], "retention_purge", f"job={job['id']}", "")
        return
    with db() as conn:
        jobs = conn.execute("SELECT * FROM jobs WHERE retention_until < ?", (now,)).fetchall()
        for job in jobs:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))
    for job in jobs:
        for folder, name in ((UPLOAD_DIR, job["stored_file"]), (OUTPUT_DIR, job["output_file"])):
            if name:
                path = folder / name
                if path.exists():
                    path.unlink()
        audit(job["user_id"], "retention_purge", f"job={job['id']}", "")


def clear_processing_history(request_user_id: str | int | None = None, client_ip: str = "") -> int:
    removed = 0
    if cloud_backend_enabled():
        docs = list(firestore_client().collection("jobs").stream())
        for doc in docs:
            job = row_from_doc(doc)
            if not job:
                continue
            delete_cloud_blob(job.get("stored_file"))
            delete_cloud_blob(job.get("output_file"))
            doc.reference.delete()
            removed += 1
    else:
        with db() as conn:
            jobs = conn.execute("SELECT * FROM jobs").fetchall()
            for job in jobs:
                for folder, name in ((UPLOAD_DIR, job["stored_file"]), (OUTPUT_DIR, job["output_file"])):
                    if name:
                        path = folder / name
                        if path.exists():
                            path.unlink()
                conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))
                removed += 1
    if request_user_id is not None:
        audit(request_user_id, "clear_history", f"processamentos_removidos={removed}", client_ip)
    return removed


def check_rate_limit(ip: str) -> bool:
    window_start = time.time() - 10 * 60
    attempts = [item for item in LOGIN_ATTEMPTS.get(ip, []) if item > window_start]
    LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) < 8


def note_login_failure(ip: str) -> None:
    LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


def validate_csrf(form: dict[str, str], session: sqlite3.Row | None) -> bool:
    return bool(session and hmac.compare_digest(form.get("csrf", ""), session["csrf_token"]))


def safe_extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Tipo de arquivo nao permitido. Use PDF, JPEG ou PNG.")
    return suffix


def upload_cloud_blob(name: str, path: Path, content_type: str = "application/octet-stream") -> None:
    storage_bucket().blob(name).upload_from_filename(str(path), content_type=content_type)


def download_cloud_blob(name: str) -> bytes | None:
    if not name:
        return None
    blob = storage_bucket().blob(name)
    if not blob.exists():
        return None
    return blob.download_as_bytes()


def delete_cloud_blob(name: str | None) -> None:
    if not name:
        return
    blob = storage_bucket().blob(name)
    if blob.exists():
        blob.delete()


def redirect(location: str, cookie: str | None = None) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [("Location", location)]
    if cookie:
        headers.append(("Set-Cookie", cookie))
    return HTTPStatus.SEE_OTHER, headers, b""


def json_response(payload: dict[str, Any], status: int = HTTPStatus.OK) -> tuple[int, list[tuple[str, str]], bytes]:
    return status, [], json.dumps(payload, ensure_ascii=False).encode("utf-8")


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LeitorSeguroOCR/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def respond(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'",
        )
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_result(self, result: tuple[int, list[tuple[str, str]], bytes]) -> None:
        status, headers, body = result
        self.respond(status, body, headers=headers)

    def respond_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.respond(status, body, "application/json; charset=utf-8")

    def current_user(self) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str | None]:
        return get_session(self.headers.get("Cookie"))

    def require_login(self, redirect_on_fail: bool = True) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        user, session, _ = self.current_user()
        if user and session:
            return user, session
        if redirect_on_fail:
            self.send_result(redirect("/login"))
        return None

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES + 4096:
            raise ValueError("Arquivo acima do limite de 25 MB.")
        return self.rfile.read(length)

    def wants_json(self) -> bool:
        accept = self.headers.get("Accept", "")
        requested_with = self.headers.get("X-Requested-With", "")
        return "application/json" in accept or requested_with.lower() == "fetch"

    def do_GET(self) -> None:
        try:
            clean_old_sessions()
        except Exception:
            pass
        try:
            purge_expired_jobs()
        except Exception:
            pass
        user, session, _ = self.current_user()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/static/styles.css":
            css = (BASE_DIR / "static" / "styles.css").read_bytes()
            self.respond(HTTPStatus.OK, css, "text/css; charset=utf-8")
            return
        if path == "/static/app.js":
            js = (BASE_DIR / "static" / "app.js").read_bytes()
            self.respond(HTTPStatus.OK, js, "application/javascript; charset=utf-8")
            return
        if path in {"/favicon.ico", "/favicon.svg"}:
            self.respond(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml; charset=utf-8")
            return
        if path == "/":
            self.send_result(redirect("/dashboard"))
            return
        if path == "/setup":
            required_token = setup_token_required()
            provided_token = query.get("token", [""])[0]
            if not has_users() and required_token and not hmac.compare_digest(provided_token, required_token):
                self.respond(
                    HTTPStatus.FORBIDDEN,
                    html_page(
                        "Setup protegido",
                        "<section class='document-page'><h1>Setup protegido</h1><p>Use o link de configuracao com token para criar o primeiro administrador.</p></section>",
                    ),
                )
                return
            self.respond(HTTPStatus.OK, setup_page(setup_token=provided_token) if not has_users() else login_page("Administrador ja configurado."))
            return
        if path == "/login":
            self.respond(HTTPStatus.OK, login_page())
            return
        if path == "/dashboard":
            if not user:
                # Cria usuario anonimo se nao existe
                if not has_users():
                    if cloud_backend_enabled():
                        create_user("Usuario", "usuario@local", "nologin123456")
                    else:
                        init_db()
                        with db() as conn:
                            conn.execute(
                                "INSERT INTO users (name, email, password_hash, role, lgpd_consent_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                ("Usuario", "usuario@local", "nologin", "admin", utc_now(), utc_now()),
                            )
                # Login automatico com primeiro usuario
                if cloud_backend_enabled():
                    user_data = get_user_by_email("usuario@local")
                    if user_data:
                        token, csrf = create_session(user_data["id"])
                        cookie = session_cookie(token)
                        _, session, _ = get_session(f"session={token}")
                        message = query.get("message", [""])[0]
                        body = dashboard_page(user_data, session, message)
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Set-Cookie", cookie)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    else:
                        self.send_result(redirect("/setup"))
                        return
                else:
                    with db() as conn:
                        user = conn.execute("SELECT * FROM users LIMIT 1").fetchone()
                    if user:
                        token, csrf = create_session(user["id"])
                        cookie = session_cookie(token)
                        _, session, _ = get_session(f"session={token}")
                        message = query.get("message", [""])[0]
                        body = dashboard_page(user, session, message)
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Set-Cookie", cookie)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    else:
                        self.send_result(redirect("/setup"))
                        return
                    return
            current_user, current_session = user, session
            message = query.get("message", [""])[0]
            self.respond(HTTPStatus.OK, dashboard_page(current_user, current_session, message))
            return
        if path == "/jobs-feed":
            required = self.require_login(redirect_on_fail=False)
            if not required:
                self.respond_json({"error": "nao_autenticado"}, HTTPStatus.UNAUTHORIZED)
                return
            current_user, current_session = required
            jobs = [_job_dict(job) for job in list_jobs_for_user(current_user["id"], 30)]
            self.respond_json(
                {
                    "jobs": jobs,
                    "summary": dashboard_summary(jobs),
                    "table_html": render_jobs_table_rows(jobs, current_session["csrf_token"]),
                    "csrf_token": current_session["csrf_token"],
                }
            )
            return
        if path == "/download":
            query = dict(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            job_id = query.get("id", [""])[0]
            if not job_id:
                self.respond(HTTPStatus.BAD_REQUEST, b"")
                return
            # Tenta com login, senao busca direto
            user_session = self.require_login(redirect_on_fail=False)
            if user_session:
                current_user, _ = user_session
                self.handle_download(current_user, query)
            else:
                self.handle_download_public(job_id)
            return
        if path == "/audit":
            required = self.require_login()
            if not required:
                return
            current_user, _ = required
            self.respond(HTTPStatus.OK, audit_page(current_user))
            return
        if path == "/privacy":
            self.respond(HTTPStatus.OK, privacy_page(user))
            return
        if path == "/security":
            self.respond(HTTPStatus.OK, security_page(user))
            return
        self.respond(HTTPStatus.NOT_FOUND, html_page("Nao encontrado", "<section class='document-page'><h1>Pagina nao encontrada</h1></section>", user))

    def do_POST(self) -> None:
        clean_old_sessions()
        purge_expired_jobs()
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/setup":
                self.handle_setup()
            elif parsed.path == "/login":
                self.handle_login()
            elif parsed.path == "/logout":
                self.handle_logout()
            elif parsed.path == "/process":
                self.handle_process()
            elif parsed.path == "/delete-job":
                self.handle_delete_job()
            elif parsed.path == "/clear-history":
                self.handle_clear_history()
            else:
                self.respond(HTTPStatus.NOT_FOUND, b"")
        except ValueError as exc:
            if self.wants_json():
                self.respond_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                self.send_result(redirect(f"/dashboard?message={urllib.parse.quote(str(exc))}"))

    def handle_setup(self) -> None:
        if has_users():
            self.send_result(redirect("/login"))
            return
        form = form_decode(self.read_body())
        required_token = setup_token_required()
        if required_token and not hmac.compare_digest(form.get("setup_token", ""), required_token):
            self.respond(HTTPStatus.FORBIDDEN, setup_page("Token de configuracao invalido."))
            return
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        confirm = form.get("confirm", "")
        if not name or not email or not password:
            self.respond(HTTPStatus.BAD_REQUEST, setup_page("Preencha todos os campos."))
            return
        if password != confirm:
            self.respond(HTTPStatus.BAD_REQUEST, setup_page("As senhas nao conferem."))
            return
        if len(password) < 10:
            self.respond(HTTPStatus.BAD_REQUEST, setup_page("Use uma senha com pelo menos 10 caracteres."))
            return
        if form.get("consent") != "yes":
            self.respond(HTTPStatus.BAD_REQUEST, setup_page("O consentimento operacional e obrigatorio."))
            return
        user_id = create_user(name, email, password)
        audit(user_id, "setup_admin", "Administrador inicial criado.", self.client_address[0])
        token, _ = create_session(user_id)
        cookie = session_cookie(token)
        self.send_result(redirect("/dashboard", cookie))

    def handle_login(self) -> None:
        ip = self.client_address[0]
        if not check_rate_limit(ip):
            self.respond(HTTPStatus.TOO_MANY_REQUESTS, login_page("Muitas tentativas. Aguarde alguns minutos."))
            return
        form = form_decode(self.read_body())
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        user = get_user_by_email(email)
        if not user or not verify_password(password, user["password_hash"]):
            note_login_failure(ip)
            audit(None, "login_failed", email, ip)
            self.respond(HTTPStatus.UNAUTHORIZED, login_page("E-mail ou senha invalidos."))
            return
        token, _ = create_session(user["id"])
        audit(user["id"], "login_success", "Login realizado.", ip)
        cookie = session_cookie(token)
        self.send_result(redirect("/dashboard", cookie))

    def handle_logout(self) -> None:
        user, _, token = self.current_user()
        if token:
            delete_session(token)
        if user:
            audit(user["id"], "logout", "Sessao encerrada.", self.client_address[0])
        cookie = session_cookie("", 0)
        self.send_result(redirect("/login", cookie))

    def handle_process(self) -> None:
        user, session, _ = self.current_user()
        if not user:
            # Auto-login com primeiro usuario
            if cloud_backend_enabled():
                user_data = get_user_by_email("usuario@local")
                if not user_data:
                    create_user("Usuario", "usuario@local", "nologin123456")
                    user_data = get_user_by_email("usuario@local")
                user = user_data
            else:
                with db() as conn:
                    user = conn.execute("SELECT * FROM users LIMIT 1").fetchone()
                    if not user:
                        conn.execute(
                            "INSERT INTO users (name, email, password_hash, role, lgpd_consent_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            ("Usuario", "usuario@local", "nologin", "admin", utc_now(), utc_now()),
                        )
                        user = conn.execute("SELECT * FROM users LIMIT 1").fetchone()
            token, _ = create_session(user["id"])
            _, session, _ = get_session(f"session={token}")
        body = self.read_body()
        form, files = parse_multipart(body, self.headers.get("Content-Type", ""))
        if session and not validate_csrf(form, session):
            raise ValueError("Token de seguranca invalido.")
        file_item = files.get("document")
        if not file_item or not file_item["content"]:
            raise ValueError("Envie um arquivo PDF, JPEG ou PNG.")
        suffix = safe_extension(file_item["filename"])
        output_format = form.get("format", "xlsx")
        if output_format not in {"xlsx", "csv"}:
            raise ValueError("Formato de saida invalido.")
        mode = form.get("mode", "table")
        if mode not in {"table", "lines"}:
            raise ValueError("Modo de leitura invalido.")
        processor = form.get("processor", "general")
        if processor not in {"general", "attendance"}:
            raise ValueError("Tipo de processamento invalido.")
        lang = form.get("lang", "por+eng")
        retention_days = max(1, min(30, int(form.get("retention_days", "7"))))
        retention_until = (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat(timespec="seconds")
        stored_name = f"{secrets.token_hex(16)}{suffix}"
        work_dir = Path(tempfile.mkdtemp(prefix="ocr_web_"))
        stored_path = work_dir / stored_name
        stored_path.write_bytes(file_item["content"])
        output_name = f"{secrets.token_hex(16)}.{output_format}"
        output_path = work_dir / output_name
        stored_ref = f"uploads/{stored_name}" if cloud_backend_enabled() else stored_name
        output_ref = f"outputs/{output_name}" if cloud_backend_enabled() else output_name
        if not cloud_backend_enabled():
            final_stored_path = UPLOAD_DIR / stored_name
            final_stored_path.write_bytes(file_item["content"])
            stored_path = final_stored_path
            output_path = OUTPUT_DIR / output_name

        job_id = create_job_record(
            user["id"],
            file_item["filename"],
            stored_ref,
            output_ref,
            output_format,
            processor if processor == "attendance" else mode,
            retention_until,
        )
        start_job_processing(
            job_id=job_id,
            user_id=user["id"],
            stored_path=stored_path,
            output_path=output_path,
            stored_ref=stored_ref,
            output_ref=output_ref,
            output_name=output_name,
            file_content_type=file_item["content_type"],
            processor=processor,
            mode=mode,
            lang=lang,
            original_name=file_item["filename"],
            client_ip=self.client_address[0],
            cleanup_dir=work_dir,
        )

        message = "Processamento iniciado. O dashboard sera atualizado automaticamente."
        if self.wants_json():
            self.respond_json(
                {
                    "ok": True,
                    "job_id": str(job_id),
                    "message": message,
                },
                HTTPStatus.ACCEPTED,
            )
            return
        self.send_result(redirect(f"/dashboard?message={urllib.parse.quote(message)}"))

    def handle_download(self, user: sqlite3.Row, query: dict[str, list[str]]) -> None:
        job_id = query.get("id", [""])[0]
        if not job_id:
            self.respond(HTTPStatus.BAD_REQUEST, b"")
            return
        job = get_job_for_user(job_id, user["id"])
        if not job or job["status"] != "concluido" or not job["output_file"]:
            self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
            return
        if cloud_backend_enabled():
            content = download_cloud_blob(job["output_file"])
            if content is None:
                self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
                return
        else:
            path = OUTPUT_DIR / job["output_file"]
            if not path.exists():
                self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
                return
            content = path.read_bytes()
        audit(user["id"], "download", f"job={job_id}", self.client_address[0])
        content_type = mimetypes.guess_type(job["output_file"])[0] or "application/octet-stream"
        filename = f"dados_extraidos_{job_id}.{job['output_format']}"
        headers = [("Content-Disposition", f'attachment; filename="{filename}"')]
        self.respond(HTTPStatus.OK, content, content_type, headers)

    def handle_download_public(self, job_id: str) -> None:
        """Download sem exigir login - busca job por ID direto."""
        if cloud_backend_enabled():
            job = None
            doc = firestore_client().collection("jobs").document(str(job_id)).get()
            if doc.exists:
                job = row_from_doc(doc)
                if job and job.get("status") != "concluido":
                    job = None
        else:
            with db() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE id = ? AND status = 'concluido'", (job_id,)).fetchone()
        if not job or not job["output_file"]:
            self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
            return
        if cloud_backend_enabled():
            content = download_cloud_blob(job["output_file"])
            if content is None:
                self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
                return
        else:
            path = OUTPUT_DIR / job["output_file"]
            if not path.exists():
                self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
                return
            content = path.read_bytes()
        content_type = mimetypes.guess_type(job["output_file"])[0] or "application/octet-stream"
        filename = f"dados_extraidos_{job_id}.{job['output_format']}"
        headers = [("Content-Disposition", f'attachment; filename="{filename}"')]
        self.respond(HTTPStatus.OK, content, content_type, headers)

    def handle_delete_job(self) -> None:
        required = self.require_login()
        if not required:
            return
        user, session = required
        form = form_decode(self.read_body())
        if not validate_csrf(form, session):
            raise ValueError("Token de seguranca invalido.")
        job_id = form.get("job_id", "")
        if not job_id:
            raise ValueError("Processamento invalido.")
        job = get_job_for_user(job_id, user["id"])
        if not job:
            raise ValueError("Processamento nao encontrado.")
        delete_job_record(job_id)
        if cloud_backend_enabled():
            delete_cloud_blob(job["stored_file"])
            delete_cloud_blob(job["output_file"])
        else:
            for folder, name in ((UPLOAD_DIR, job["stored_file"]), (OUTPUT_DIR, job["output_file"])):
                if name:
                    path = folder / name
                    if path.exists():
                        path.unlink()
        audit(user["id"], "delete_job", f"job={job_id}", self.client_address[0])
        self.send_result(redirect(f"/dashboard?message={urllib.parse.quote('Arquivo excluido.')}"))

    def handle_clear_history(self) -> None:
        required = self.require_login()
        if not required:
            return
        user, session = required
        form = form_decode(self.read_body())
        if not validate_csrf(form, session):
            raise ValueError("Token de seguranca invalido.")
        removed = clear_processing_history(user["id"], self.client_address[0])
        self.send_result(
            redirect(
                f"/dashboard?message={urllib.parse.quote(f'Historico limpo com sucesso. Itens removidos: {removed}.')}"
            )
        )


def main() -> None:
    init_db()
    host = os.environ.get("OCR_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCR_WEB_PORT", os.environ.get("PORT", "8000")))
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"{APP_NAME} rodando em http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

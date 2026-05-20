from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from assinatura_lista import analyze_attendance_files
from extrator_ocr import extract_rows, write_output


APP_NAME = "Leitor Seguro OCR"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "app.sqlite3"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SESSION_TTL_SECONDS = 8 * 60 * 60
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


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
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, csrf, expires_at, utc_now()),
        )
    return token, csrf


def delete_session(token: str) -> None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
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
    with db() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (token_hash,)).fetchone()
        if not session or session["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM sessions WHERE id = ?", (token_hash,))
            return None, None, None
        user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return user, session, token


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
      <link rel="stylesheet" href="/static/styles.css">
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


def setup_page(message: str = "") -> bytes:
    body = f"""
    <section class="auth-shell">
      <div class="auth-panel">
        <p class="eyebrow">Primeiro acesso</p>
        <h1>Crie o administrador</h1>
        {alert(message, "error" if message else "info")}
        <form class="form-grid" method="post" action="/setup">
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
    with db() as conn:
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT 30",
            (user["id"],),
        ).fetchall()
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
      <div>
        <p class="eyebrow">Area segura</p>
        <h1>Extrair dados de PDF, JPEG ou PNG</h1>
      </div>
      <div class="identity">
        <span>{escape(user["name"])}</span>
        <strong>{escape(user["email"])}</strong>
      </div>
    </section>

    {alert(message, "success" if "Concluido" in message else "error" if message else "info")}

    <section class="workspace">
      <form class="upload-panel" method="post" action="/process" enctype="multipart/form-data">
        <input type="hidden" name="csrf" value="{escape(session["csrf_token"])}">
        <label>Arquivo
          <input name="document" type="file" accept=".pdf,.jpg,.jpeg,.png" required>
        </label>
        <div class="field-row">
          <label>Tipo
            <select name="processor">
              <option value="general">OCR geral</option>
              <option value="attendance">Lista de presenca / assinaturas</option>
            </select>
          </label>
          <label>Saida
            <select name="format">
              <option value="xlsx">XLSX</option>
              <option value="csv">CSV</option>
            </select>
          </label>
          <label>Leitura OCR geral
            <select name="mode">
              <option value="table">Tabela simples</option>
              <option value="lines">Linhas de texto</option>
            </select>
          </label>
        </div>
        <div class="field-row">
          <label>Idioma OCR
            <select name="lang">
              <option value="por+eng">Portugues + Ingles</option>
              <option value="por">Portugues</option>
              <option value="eng">Ingles</option>
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
        <label class="check">
          <input name="purpose" type="checkbox" value="yes" required>
          <span>Confirmo que tenho base legal para tratar este documento e que ele sera usado apenas para extracao dos dados.</span>
        </label>
        <button type="submit">Processar com seguranca</button>
      </form>

      <aside class="compliance-card">
        <h2>LGPD na pratica</h2>
        <dl>
          <dt>Finalidade</dt><dd>Extracao OCR informada antes do envio.</dd>
          <dt>Minimizacao</dt><dd>Apenas PDF/JPEG/PNG ate 25 MB.</dd>
          <dt>Retencao</dt><dd>Prazo definido a cada processamento.</dd>
          <dt>Rastreabilidade</dt><dd>Eventos gravados em auditoria local.</dd>
          <dt>Assinatura</dt><dd>Modo especifico para detectar tinta manuscrita por celula.</dd>
          <dt>Cabecalho</dt><dd>Extrai curso, modulo, turma e data quando o OCR estiver disponivel.</dd>
        </dl>
      </aside>
    </section>

    <section class="history">
      <h2>Processamentos recentes</h2>
      <table>
        <thead>
          <tr><th>Arquivo</th><th>Status</th><th>Linhas</th><th>Formato</th><th>Retencao</th><th>Acoes</th></tr>
        </thead>
        <tbody>{table}</tbody>
      </table>
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
    with db() as conn:
        entries = conn.execute(
            """
            SELECT audit_log.*, users.email
            FROM audit_log
            LEFT JOIN users ON users.id = audit_log.user_id
            ORDER BY audit_log.id DESC
            LIMIT 100
            """
        ).fetchall()
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
      <table>
        <thead>
          <tr><th>Data</th><th>Usuario</th><th>Evento</th><th>Detalhes</th><th>IP</th></tr>
        </thead>
        <tbody>{table}</tbody>
      </table>
    </section>
    """
    return html_page("Auditoria", body, user)


def clean_old_sessions() -> None:
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def purge_expired_jobs() -> None:
    now = utc_now()
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


def redirect(location: str, cookie: str | None = None) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = [("Location", location)]
    if cookie:
        headers.append(("Set-Cookie", cookie))
    return HTTPStatus.SEE_OTHER, headers, b""


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

    def current_user(self) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str | None]:
        return get_session(self.headers.get("Cookie"))

    def require_login(self) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        user, session, _ = self.current_user()
        if user and session:
            return user, session
        self.send_result(redirect("/login"))
        return None

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES + 4096:
            raise ValueError("Arquivo acima do limite de 25 MB.")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        clean_old_sessions()
        purge_expired_jobs()
        user, session, _ = self.current_user()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/static/styles.css":
            css = (BASE_DIR / "static" / "styles.css").read_bytes()
            self.respond(HTTPStatus.OK, css, "text/css; charset=utf-8")
            return
        if path == "/":
            self.send_result(redirect("/dashboard" if user else "/setup" if not has_users() else "/login"))
            return
        if path == "/setup":
            self.respond(HTTPStatus.OK, setup_page() if not has_users() else login_page("Administrador ja configurado."))
            return
        if path == "/login":
            self.respond(HTTPStatus.OK, login_page())
            return
        if path == "/dashboard":
            required = self.require_login()
            if not required:
                return
            current_user, current_session = required
            message = query.get("message", [""])[0]
            self.respond(HTTPStatus.OK, dashboard_page(current_user, current_session, message))
            return
        if path == "/download":
            required = self.require_login()
            if not required:
                return
            current_user, _ = required
            self.handle_download(current_user, query)
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
            else:
                self.respond(HTTPStatus.NOT_FOUND, b"")
        except ValueError as exc:
            self.send_result(redirect(f"/dashboard?message={urllib.parse.quote(str(exc))}"))

    def handle_setup(self) -> None:
        if has_users():
            self.send_result(redirect("/login"))
            return
        form = form_decode(self.read_body())
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
        with db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (name, email, password_hash, lgpd_consent_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, hash_password(password), utc_now(), utc_now()),
            )
            user_id = int(cursor.lastrowid)
        audit(user_id, "setup_admin", "Administrador inicial criado.", self.client_address[0])
        token, _ = create_session(user_id)
        cookie = f"session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
        self.send_result(redirect("/dashboard", cookie))

    def handle_login(self) -> None:
        ip = self.client_address[0]
        if not check_rate_limit(ip):
            self.respond(HTTPStatus.TOO_MANY_REQUESTS, login_page("Muitas tentativas. Aguarde alguns minutos."))
            return
        form = form_decode(self.read_body())
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            note_login_failure(ip)
            audit(None, "login_failed", email, ip)
            self.respond(HTTPStatus.UNAUTHORIZED, login_page("E-mail ou senha invalidos."))
            return
        token, _ = create_session(user["id"])
        audit(user["id"], "login_success", "Login realizado.", ip)
        cookie = f"session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL_SECONDS}"
        self.send_result(redirect("/dashboard", cookie))

    def handle_logout(self) -> None:
        user, _, token = self.current_user()
        if token:
            delete_session(token)
        if user:
            audit(user["id"], "logout", "Sessao encerrada.", self.client_address[0])
        cookie = "session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        self.send_result(redirect("/login", cookie))

    def handle_process(self) -> None:
        required = self.require_login()
        if not required:
            return
        user, session = required
        body = self.read_body()
        form, files = parse_multipart(body, self.headers.get("Content-Type", ""))
        if not validate_csrf(form, session):
            raise ValueError("Token de seguranca invalido. Atualize a pagina e tente novamente.")
        if form.get("purpose") != "yes":
            raise ValueError("Confirme a base legal/finalidade antes de processar.")
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
        stored_path = UPLOAD_DIR / stored_name
        stored_path.write_bytes(file_item["content"])
        output_name = f"{secrets.token_hex(16)}.{output_format}"
        output_path = OUTPUT_DIR / output_name
        created_at = utc_now()

        with db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (user_id, original_name, stored_file, output_file, output_format, mode, status, retention_until, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user["id"], file_item["filename"], stored_name, output_name, output_format, processor if processor == "attendance" else mode, "processando", retention_until, created_at),
            )
            job_id = int(cursor.lastrowid)

        try:
            if processor == "attendance":
                row_count = analyze_attendance_files([stored_path], output_path, lang=lang)
            else:
                rows = extract_rows([stored_path], lang=lang, psm="6", dpi=300, mode=mode)
                write_output(rows, output_path)
                row_count = len(rows)
            with db() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, rows_count = ? WHERE id = ?",
                    ("concluido", row_count, job_id),
                )
            audit(user["id"], "process_success", f"job={job_id}; arquivo={file_item['filename']}", self.client_address[0])
            self.send_result(redirect(f"/dashboard?message={urllib.parse.quote('Concluido: arquivo processado com sucesso.')}"))
        except BaseException as exc:
            message = str(exc) or exc.__class__.__name__
            with db() as conn:
                conn.execute("UPDATE jobs SET status = ?, error = ? WHERE id = ?", ("erro", message[:1000], job_id))
            audit(user["id"], "process_error", f"job={job_id}; erro={message[:500]}", self.client_address[0])
            self.send_result(redirect(f"/dashboard?message={urllib.parse.quote(message)}"))

    def handle_download(self, user: sqlite3.Row, query: dict[str, list[str]]) -> None:
        try:
            job_id = int(query.get("id", ["0"])[0])
        except ValueError:
            self.respond(HTTPStatus.BAD_REQUEST, b"")
            return
        with db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
        if not job or job["status"] != "concluido" or not job["output_file"]:
            self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
            return
        path = OUTPUT_DIR / job["output_file"]
        if not path.exists():
            self.respond(HTTPStatus.NOT_FOUND, b"Arquivo nao encontrado.")
            return
        audit(user["id"], "download", f"job={job_id}", self.client_address[0])
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = f"dados_extraidos_{job_id}.{job['output_format']}"
        headers = [("Content-Disposition", f'attachment; filename="{filename}"')]
        self.respond(HTTPStatus.OK, path.read_bytes(), content_type, headers)

    def handle_delete_job(self) -> None:
        required = self.require_login()
        if not required:
            return
        user, session = required
        form = form_decode(self.read_body())
        if not validate_csrf(form, session):
            raise ValueError("Token de seguranca invalido.")
        try:
            job_id = int(form.get("job_id", "0"))
        except ValueError:
            raise ValueError("Processamento invalido.")
        with db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
            if not job:
                raise ValueError("Processamento nao encontrado.")
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        for folder, name in ((UPLOAD_DIR, job["stored_file"]), (OUTPUT_DIR, job["output_file"])):
            if name:
                path = folder / name
                if path.exists():
                    path.unlink()
        audit(user["id"], "delete_job", f"job={job_id}", self.client_address[0])
        self.send_result(redirect(f"/dashboard?message={urllib.parse.quote('Arquivo excluido.')}"))


def main() -> None:
    init_db()
    host = os.environ.get("OCR_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCR_WEB_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"{APP_NAME} rodando em http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

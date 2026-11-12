from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import sqlite3
import unicodedata
from base64 import b64decode
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pypdf import PdfReader, PdfWriter
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.document_scanner import (
    DocumentRejected,
    MAX_UPLOAD_BYTES,
    TextPart,
    inspect_document,
    scan_personal_information,
)
from app.handbook_data import HANDBOOK_CATEGORIES
from app.permission_data import PERMISSION_DOCUMENTS
from app.sample_data import SAMPLE_DOCUMENTS

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
STORAGE_DIR = DATA_DIR / "documents"
PERMISSION_STORAGE_DIR = DATA_DIR / "permission_attachments"
HANDBOOK_STORAGE_DIR = DATA_DIR / "handbook_attachments"
DB_PATH = DATA_DIR / "manuals.db"
SOURCE_MANUAL_PATH = Path(os.getenv("SOURCE_MANUAL_PATH", DATA_DIR / "source-manual.pdf"))

DEFAULT_RELATED_SITES = [
    ("국가법령정보센터", "https://www.law.go.kr/LSW/main.html"),
    ("나의 사건 검색", "https://www.scourt.go.kr/portal/information/events/search/search.jsp"),
    ("웹팩스", "http://102.6.1.119:8080/WebFax/users/login"),
    ("모바일 신분증 진위확인·사본저장 시스템", "https://ive.mobileid.go.kr/userLogin.do"),
]
HANDBOOK_RECORDED_FINDINGS = frozenset({"주민·외국인등록번호", "휴대전화번호", "주소"})

HEADING_PATTERN = re.compile(
    r"^(?:(?P<chapter>[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\.)|(?P<section>▣\s*)|(?P<number>\d+\.\s+)|(?P<sub>\d+\)\s+))(?P<title>.+)$"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutlineItem(StrictModel):
    level: int
    title: str
    anchor: str
    position: int


class DocumentSummary(StrictModel):
    id: int
    slug: str
    title: str
    category: str
    department: str
    summary: str
    status: str
    version: int
    updated_at: str


class DocumentDetail(DocumentSummary):
    content: str
    outline: list[OutlineItem]
    original_filename: str | None
    file_type: str
    size_bytes: int
    page_count: int
    downloadable: bool


class DocumentList(StrictModel):
    items: list[DocumentSummary]
    total: int
    page: int
    page_size: int


class VersionItem(StrictModel):
    id: int
    version: int
    change_note: str
    original_filename: str | None
    file_type: str
    size_bytes: int
    page_count: int
    created_at: str
    created_by: str


def decode_admin_key(value: str) -> str:
    if not value.startswith("b64:"):
        return value
    try:
        return b64decode(value[4:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def admin_key_matches(provided: str, configured: str) -> bool:
    return bool(provided) and secrets.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = None
        if request.url.path.startswith("/api/admin"):
            configured_key = os.getenv("ADMIN_KEY")
            provided_key = decode_admin_key(request.headers.get("X-Admin-Key", ""))
            if not configured_key:
                response = JSONResponse({"detail": "관리자 업로드 키가 설정되지 않았습니다."}, status_code=503)
            elif not admin_key_matches(provided_key, configured_key):
                response = JSONResponse({"detail": "관리자 인증에 실패했습니다."}, status_code=401)
            else:
                content_length = request.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                    response = JSONResponse({"detail": "요청 크기는 31MB를 초과할 수 없습니다."}, status_code=413)
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'self'; form-action 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
        )
        if request.url.path.startswith("/api/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response


class RequestBodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            response = JSONResponse({"detail": "요청 크기는 31MB를 초과할 수 없습니다."}, status_code=413)
            await response(scope, receive, send)


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_metadata(value: str, field: str, max_length: int, required: bool = True) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if required and not normalized:
        raise HTTPException(status_code=422, detail=f"{field} 항목을 입력하세요.")
    if len(normalized) > max_length:
        raise HTTPException(status_code=422, detail=f"{field} 항목은 {max_length}자 이하여야 합니다.")
    return normalized


def build_outline(text: str) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    used: set[str] = set()
    for position, raw_line in enumerate(text.splitlines()):
        line = re.sub(r"\s+", " ", raw_line).strip()
        match = HEADING_PATTERN.match(line)
        if not match or len(line) > 100:
            continue
        level = 1 if match.group("chapter") else 2 if match.group("section") or match.group("number") else 3
        title = line
        base = re.sub(r"[^0-9A-Za-z가-힣]+", "-", title).strip("-").lower() or f"section-{position}"
        anchor = base
        suffix = 2
        while anchor in used:
            anchor = f"{base}-{suffix}"
            suffix += 1
        used.add(anchor)
        outline.append({"level": level, "title": title, "anchor": anchor, "position": position})
    return outline[:200]


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    PERMISSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    HANDBOOK_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                department TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('published', 'archived')),
                current_version_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                version_no INTEGER NOT NULL,
                change_note TEXT NOT NULL DEFAULT '',
                original_filename TEXT,
                stored_filename TEXT,
                file_type TEXT NOT NULL,
                media_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT,
                extracted_text TEXT NOT NULL,
                page_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(document_id, version_no)
            );

            CREATE TABLE IF NOT EXISTS document_headings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
                level INTEGER NOT NULL,
                title TEXT NOT NULL,
                anchor TEXT NOT NULL,
                position INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS upload_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                result TEXT NOT NULL,
                reason_code TEXT,
                detected_types TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS permission_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                permission_slug TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL CHECK(file_type IN ('hwp', 'hwpx')),
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handbook_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_slug TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL CHECK(file_type IN ('hwp', 'hwpx', 'pdf', 'docx')),
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS related_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                display_order INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS document_search USING fts5(
                document_id UNINDEXED,
                title,
                category,
                department,
                summary,
                content,
                tokenize='unicode61'
            );

            CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category, status);
            CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id, version_no DESC);
            CREATE INDEX IF NOT EXISTS idx_audits_created ON upload_audits(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_permission_attachments_slug
                ON permission_attachments(permission_slug, id DESC);
            CREATE INDEX IF NOT EXISTS idx_handbook_attachments_slug
                ON handbook_attachments(category_slug, id DESC);
            CREATE INDEX IF NOT EXISTS idx_related_sites_order
                ON related_sites(display_order, id);
            """
        )
        if connection.execute("SELECT COUNT(*) FROM related_sites").fetchone()[0] == 0:
            created_at = now_iso()
            connection.executemany(
                """INSERT INTO related_sites(name, url, display_order, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (name, url, index, created_at, "기본 설정")
                    for index, (name, url) in enumerate(DEFAULT_RELATED_SITES, start=1)
                ],
            )
        if connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0 and os.getenv("LOAD_SAMPLE_DATA", "true").lower() == "true":
            seed_sample_documents(connection)


def seed_sample_documents(connection: sqlite3.Connection) -> None:
    created_at = now_iso()
    for index, item in enumerate(SAMPLE_DOCUMENTS, start=1):
        slug = f"guide-{index:02d}"
        cursor = connection.execute(
            """INSERT INTO documents(slug, title, category, department, summary, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (slug, item["title"], item["category"], item["department"], item["summary"], created_at, created_at),
        )
        document_id = cursor.lastrowid
        cursor = connection.execute(
            """INSERT INTO document_versions(
                   document_id, version_no, change_note, file_type, extracted_text, page_count, created_at, created_by
               ) VALUES (?, 1, ?, 'sample', ?, 1, ?, '시스템')""",
            (document_id, "초기 매뉴얼 등록", item["content"], created_at),
        )
        version_id = cursor.lastrowid
        connection.execute("UPDATE documents SET current_version_id = ? WHERE id = ?", (version_id, document_id))
        insert_headings(connection, version_id, item["content"])
        connection.execute(
            "INSERT INTO document_search VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, item["title"], item["category"], item["department"], item["summary"], item["content"]),
        )


def insert_headings(connection: sqlite3.Connection, version_id: int, text: str) -> None:
    headings = build_outline(text)
    connection.executemany(
        "INSERT INTO document_headings(version_id, level, title, anchor, position) VALUES (?, ?, ?, ?, ?)",
        [(version_id, item["level"], item["title"], item["anchor"], item["position"]) for item in headings],
    )


def audit_upload(filename: str, result: str, reason_code: str, detected_types: str, digest: str, actor: str) -> None:
    with db_connect() as connection:
        connection.execute(
            """INSERT INTO upload_audits(filename, result, reason_code, detected_types, sha256, actor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (filename, result, reason_code, detected_types, digest, actor, now_iso()),
        )


def persist_permission_attachment(
    *,
    permission_slug: str,
    content: bytes,
    inspection,
    filename: str,
    actor: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    stored_filename = f"{digest}-{uuid4().hex[:8]}.{inspection.file_type}"
    destination = PERMISSION_STORAGE_DIR / stored_filename
    temporary = PERMISSION_STORAGE_DIR / f".{stored_filename}.tmp"
    try:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        created_at = now_iso()
        with db_connect() as connection:
            cursor = connection.execute(
                """INSERT INTO permission_attachments(
                       permission_slug, original_filename, stored_filename, file_type,
                       media_type, size_bytes, sha256, created_at, created_by
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    permission_slug, filename, stored_filename, inspection.file_type,
                    inspection.media_type, len(content), digest, created_at, actor,
                ),
            )
            attachment_id = cursor.lastrowid
        return {
            "id": attachment_id,
            "permission_slug": permission_slug,
            "original_filename": filename,
            "file_type": inspection.file_type,
            "size_bytes": len(content),
            "created_at": created_at,
            "created_by": actor,
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def persist_handbook_attachment(
    *,
    category_slug: str,
    content: bytes,
    inspection,
    filename: str,
    actor: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    stored_filename = f"{digest}-{uuid4().hex[:8]}.{inspection.file_type}"
    destination = HANDBOOK_STORAGE_DIR / stored_filename
    temporary = HANDBOOK_STORAGE_DIR / f".{stored_filename}.tmp"
    try:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        created_at = now_iso()
        with db_connect() as connection:
            cursor = connection.execute(
                """INSERT INTO handbook_attachments(
                       category_slug, original_filename, stored_filename, file_type,
                       media_type, size_bytes, sha256, created_at, created_by
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    category_slug, filename, stored_filename, inspection.file_type,
                    inspection.media_type, len(content), digest, created_at, actor,
                ),
            )
            attachment_id = cursor.lastrowid
        return {
            "id": attachment_id,
            "category_slug": category_slug,
            "original_filename": filename,
            "file_type": inspection.file_type,
            "size_bytes": len(content),
            "created_at": created_at,
            "created_by": actor,
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


def remove_handbook_attachment(attachment_id: int) -> bool:
    with db_connect() as connection:
        row = connection.execute(
            "SELECT stored_filename FROM handbook_attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
    if not row:
        return False
    path = HANDBOOK_STORAGE_DIR / Path(row["stored_filename"]).name
    temporary = HANDBOOK_STORAGE_DIR / f".{path.name}.deleting"
    if path.is_file():
        os.replace(path, temporary)
    try:
        with db_connect() as connection:
            connection.execute("DELETE FROM handbook_attachments WHERE id = ?", (attachment_id,))
        temporary.unlink(missing_ok=True)
        return True
    except Exception:
        if temporary.is_file():
            os.replace(temporary, path)
        raise


def row_to_summary(row: sqlite3.Row) -> DocumentSummary:
    return DocumentSummary(
        id=row["id"], slug=row["slug"], title=row["title"], category=row["category"],
        department=row["department"], summary=row["summary"], status=row["status"],
        version=row["version_no"], updated_at=row["updated_at"],
    )


def base_document_query() -> str:
    return """
        SELECT d.id, d.slug, d.title, d.category, d.department, d.summary, d.status,
               d.current_version_id, d.updated_at,
               v.version_no, v.original_filename, v.file_type, v.size_bytes, v.page_count,
               v.extracted_text, v.stored_filename, v.media_type
        FROM documents d
        JOIN document_versions v ON v.id = d.current_version_id
    """


def get_document_row(identifier: str | int) -> sqlite3.Row:
    clause = "d.id = ?" if isinstance(identifier, int) or str(identifier).isdigit() else "d.slug = ?"
    with db_connect() as connection:
        row = connection.execute(f"{base_document_query()} WHERE {clause}", (identifier,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    return row


async def require_admin(
    x_admin_key: Annotated[str | None, Header()] = None,
) -> None:
    configured_key = os.getenv("ADMIN_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="관리자 업로드 키가 설정되지 않았습니다.")
    provided_key = decode_admin_key(x_admin_key or "")
    if not admin_key_matches(provided_key, configured_key):
        raise HTTPException(status_code=401, detail="관리자 인증에 실패했습니다.")


def persist_document(
    *,
    content: bytes,
    inspection,
    filename: str,
    title: str,
    category: str,
    department: str,
    summary: str,
    change_note: str,
    existing_document_id: int | None,
    actor: str,
) -> tuple[int, int, int]:
    digest = hashlib.sha256(content).hexdigest()
    stored_filename = f"{digest}-{uuid4().hex[:8]}.{inspection.file_type}"
    destination = STORAGE_DIR / stored_filename
    temporary = STORAGE_DIR / f".{stored_filename}.tmp"
    try:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        with db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created_at = now_iso()
            if existing_document_id is None:
                slug = f"manual-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:8]}"
                cursor = connection.execute(
                    """INSERT INTO documents(slug, title, category, department, summary, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (slug, title, category, department, summary, created_at, created_at),
                )
                document_id = cursor.lastrowid
                version_no = 1
            else:
                document = connection.execute("SELECT id FROM documents WHERE id = ?", (existing_document_id,)).fetchone()
                if not document:
                    raise HTTPException(status_code=404, detail="개정할 기존 문서를 찾을 수 없습니다.")
                document_id = existing_document_id
                version_no = connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM document_versions WHERE document_id = ?",
                    (document_id,),
                ).fetchone()[0]
                connection.execute(
                    """UPDATE documents SET title = ?, category = ?, department = ?, summary = ?,
                       status = 'published', updated_at = ? WHERE id = ?""",
                    (title, category, department, summary, created_at, document_id),
                )

            cursor = connection.execute(
                """INSERT INTO document_versions(
                       document_id, version_no, change_note, original_filename, stored_filename, file_type,
                       media_type, size_bytes, sha256, extracted_text, page_count, created_at, created_by
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id, version_no, change_note, filename, stored_filename, inspection.file_type,
                    inspection.media_type, len(content), digest, inspection.text, inspection.page_count,
                    created_at, actor,
                ),
            )
            version_id = cursor.lastrowid
            insert_headings(connection, version_id, inspection.text)
            connection.execute(
                "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, created_at, document_id),
            )
            connection.execute("DELETE FROM document_search WHERE document_id = ?", (document_id,))
            connection.execute(
                "INSERT INTO document_search VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, title, category, department, summary, inspection.text),
            )
            connection.execute(
                """INSERT INTO upload_audits(filename, result, reason_code, detected_types, sha256, actor, created_at)
                   VALUES (?, 'accepted', '', '', ?, ?, ?)""",
                (filename, digest, actor, created_at),
            )
        return document_id, version_id, version_no
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="통합민원대 업무지원서비스 API",
    description="통합민원대 업무자료 검색, 버전 관리, 개인정보 검사 업로드 API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_UPLOAD_BYTES + 1024 * 1024)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
@app.get("/admin.html", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/styles.css", include_in_schema=False)
async def styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")


@app.get("/app.js", include_in_schema=False)
async def javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


@app.get("/admin.js", include_in_schema=False)
async def admin_javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.js", media_type="text/javascript")


def extract_source_pages(start_page: int, end_page: int) -> bytes:
    reader = PdfReader(SOURCE_MANUAL_PATH, strict=False)
    if end_page > len(reader.pages):
        raise HTTPException(status_code=404, detail="요청한 원문 페이지를 찾을 수 없습니다.")
    writer = PdfWriter()
    for page_number in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_number])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@app.get("/source-manual.pdf", include_in_schema=False)
async def source_manual(
    start: int | None = Query(None, ge=1),
    end: int | None = Query(None, ge=1),
) -> Response:
    if not SOURCE_MANUAL_PATH.is_file():
        raise HTTPException(status_code=404, detail="원본 업무매뉴얼 PDF를 찾을 수 없습니다.")
    if start is not None:
        end = end or start
        if end < start or end - start > 50:
            raise HTTPException(status_code=422, detail="원문 페이지 범위가 올바르지 않습니다.")
        content = await run_in_threadpool(extract_source_pages, start, end)
        return Response(
            content,
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="source-manual-pages.pdf"'},
        )
    return FileResponse(
        SOURCE_MANUAL_PATH,
        media_type="application/pdf",
        filename="통합민원-업무매뉴얼.pdf",
        content_disposition_type="inline",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def rewrite_law_content(text: str, content_type: str) -> str:
    text = text.replace("https://www.law.go.kr/", "/law-proxy/")
    text = text.replace("https://law.go.kr/", "/law-proxy/")
    text = text.replace("//www.law.go.kr/", "/law-proxy/")
    if "text/html" in content_type or "javascript" in content_type:
        text = re.sub(r"([\"'])/(?!/|law-proxy/)", r"\1/law-proxy/", text)
    if "text/css" in content_type:
        text = re.sub(r"url\((['\"]?)/(?!/|law-proxy/)", r"url(\1/law-proxy/", text)
    return text


@app.api_route("/law-proxy/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def law_proxy(path: str, request: Request) -> Response:
    if not path or any(part == ".." for part in Path(path).parts):
        raise HTTPException(status_code=404, detail="국가법령정보센터 경로를 찾을 수 없습니다.")
    target = f"https://www.law.go.kr/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    forward_headers = {
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
        "Accept": request.headers.get("accept", "*/*"),
    }
    for header in ("content-type", "cookie"):
        if request.headers.get(header):
            forward_headers[header.title()] = request.headers[header]
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            upstream = await client.request(
                request.method,
                target,
                headers=forward_headers,
                content=await request.body(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="국가법령정보센터에 연결할 수 없습니다.") from exc
    if upstream.url.host not in {"law.go.kr", "www.law.go.kr"}:
        raise HTTPException(status_code=502, detail="허용되지 않은 외부 주소로 이동했습니다.")
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    response_headers: dict[str, str] = {}
    if upstream.headers.get("set-cookie"):
        response_headers["set-cookie"] = upstream.headers["set-cookie"]
    disposition = upstream.headers.get("content-disposition")
    if disposition and disposition.isascii():
        response_headers["content-disposition"] = disposition
    if any(kind in content_type for kind in ("text/html", "text/css", "javascript")):
        rewritten = rewrite_law_content(upstream.text, content_type)
        if "text/html" in content_type:
            return HTMLResponse(rewritten, status_code=upstream.status_code, headers=response_headers)
        return Response(rewritten, status_code=upstream.status_code, media_type=content_type.split(";", 1)[0], headers=response_headers)
    return Response(upstream.content, status_code=upstream.status_code, media_type=content_type.split(";", 1)[0], headers=response_headers)


@app.get("/api/categories")
async def list_categories() -> dict[str, list[dict[str, Any]]]:
    with db_connect() as connection:
        rows = connection.execute(
            """SELECT category, COUNT(*) AS count FROM documents
               WHERE status = 'published' GROUP BY category ORDER BY MIN(id)"""
        ).fetchall()
    return {"items": [{"name": row["category"], "count": row["count"]} for row in rows]}


@app.get("/api/related-sites")
async def list_related_sites() -> dict[str, list[dict[str, Any]]]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT id, name, url FROM related_sites ORDER BY display_order, id"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/admin/related-sites")
async def add_related_site(
    name: Annotated[str, Form(...)],
    url: Annotated[str, Form(...)],
    admin_name: Annotated[str, Form()] = "담당자",
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    normalized_name = normalize_metadata(name, "사이트명", 100)
    normalized_url = normalize_metadata(url, "사이트 주소", 500)
    actor = normalize_metadata(admin_name, "담당자명", 50)
    parsed = urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="http 또는 https로 시작하는 올바른 사이트 주소를 입력하세요.")
    created_at = now_iso()
    try:
        with db_connect() as connection:
            display_order = connection.execute(
                "SELECT COALESCE(MAX(display_order), 0) + 1 FROM related_sites"
            ).fetchone()[0]
            cursor = connection.execute(
                """INSERT INTO related_sites(name, url, display_order, created_at, created_by)
                   VALUES (?, ?, ?, ?, ?)""",
                (normalized_name, normalized_url, display_order, created_at, actor),
            )
            site_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="이미 등록된 사이트 주소입니다.") from exc
    return {
        "message": "관련사이트를 추가했습니다.",
        "site": {"id": site_id, "name": normalized_name, "url": normalized_url},
    }


@app.get("/api/handbooks")
async def list_handbooks() -> dict[str, list[dict[str, Any]]]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT category_slug, COUNT(*) AS count FROM handbook_attachments GROUP BY category_slug"
        ).fetchall()
    counts = {row["category_slug"]: row["count"] for row in rows}
    return {"items": [
        {**category, "attachment_count": counts.get(category["slug"], 0)}
        for category in HANDBOOK_CATEGORIES
    ]}


@app.get("/api/handbooks/{slug}")
async def handbook_detail(slug: str) -> dict[str, Any]:
    category = next((item for item in HANDBOOK_CATEGORIES if item["slug"] == slug), None)
    if not category:
        raise HTTPException(status_code=404, detail="편람 분류를 찾을 수 없습니다.")
    with db_connect() as connection:
        rows = connection.execute(
            """SELECT id, original_filename, file_type, size_bytes, created_at, created_by
               FROM handbook_attachments WHERE category_slug = ? ORDER BY id DESC""",
            (slug,),
        ).fetchall()
    return {**category, "attachments": [dict(row) for row in rows]}


@app.get("/api/handbook-attachments/{attachment_id}/download")
async def download_handbook_attachment(attachment_id: int) -> FileResponse:
    with db_connect() as connection:
        row = connection.execute(
            """SELECT original_filename, stored_filename, media_type
               FROM handbook_attachments WHERE id = ?""",
            (attachment_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="편람 첨부파일을 찾을 수 없습니다.")
    path = HANDBOOK_STORAGE_DIR / Path(row["stored_filename"]).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="편람 첨부파일 원본을 찾을 수 없습니다.")
    return FileResponse(path, media_type=row["media_type"], filename=row["original_filename"])


@app.get("/api/handbook-attachments/{attachment_id}/view")
async def view_handbook_attachment(attachment_id: int) -> FileResponse:
    with db_connect() as connection:
        row = connection.execute(
            """SELECT original_filename, stored_filename, file_type
               FROM handbook_attachments WHERE id = ?""",
            (attachment_id,),
        ).fetchone()
    if not row or row["file_type"] != "pdf":
        raise HTTPException(status_code=404, detail="바로 볼 수 있는 PDF 편람을 찾을 수 없습니다.")
    path = HANDBOOK_STORAGE_DIR / Path(row["stored_filename"]).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="편람 PDF 원본을 찾을 수 없습니다.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["original_filename"],
        content_disposition_type="inline",
    )


@app.post("/api/admin/handbook-attachments")
async def upload_handbook_attachment(
    file: Annotated[UploadFile, File(...)],
    category_slug: Annotated[str, Form(...)],
    admin_name: Annotated[str, Form()] = "담당자",
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    if not any(item["slug"] == category_slug for item in HANDBOOK_CATEGORIES):
        raise HTTPException(status_code=404, detail="편람 분류를 찾을 수 없습니다.")
    actor = normalize_metadata(admin_name, "담당자명", 50)
    filename = normalize_metadata(Path(file.filename or "").name, "파일명", 255)
    if Path(filename).suffix.lower() not in {".hwp", ".hwpx", ".pdf", ".docx"}:
        raise HTTPException(status_code=422, detail="편람은 HWP, HWPX, PDF 또는 DOCX 파일만 등록할 수 있습니다.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    digest = hashlib.sha256(content).hexdigest()
    audit_filename = f"handbook-{digest[:12]}{Path(filename).suffix.lower()}"
    metadata_findings = tuple(
        finding
        for finding in scan_personal_information([TextPart("첨부 정보", f"{filename}\n{actor}")])
        if finding.kind in HANDBOOK_RECORDED_FINDINGS
    )
    try:
        inspection = await run_in_threadpool(
            inspect_document,
            filename,
            content,
            allow_pdf_images=True,
            allow_pdf_actions=True,
            allow_document_images=True,
            blocked_finding_kinds=HANDBOOK_RECORDED_FINDINGS,
            allow_blocked_findings=True,
        )
    except DocumentRejected as exc:
        detected = ", ".join(sorted({finding.kind for finding in exc.findings}))
        await run_in_threadpool(audit_upload, audit_filename, "blocked", exc.code, detected, digest, actor)
        raise HTTPException(status_code=422, detail={
            "code": exc.code,
            "message": exc.message,
            "findings": [
                {"type": finding.kind, "location": finding.location, "count": finding.count}
                for finding in exc.findings[:30]
            ],
        }) from exc
    if inspection.file_type not in {"hwp", "hwpx", "pdf", "docx"}:
        raise HTTPException(status_code=422, detail="편람은 HWP, HWPX, PDF 또는 DOCX 파일만 등록할 수 있습니다.")
    try:
        attachment = await run_in_threadpool(
            persist_handbook_attachment,
            category_slug=category_slug,
            content=content,
            inspection=inspection,
            filename=filename,
            actor=actor,
        )
        recorded_findings = (*metadata_findings, *inspection.findings)
        detected = ", ".join(sorted({finding.kind for finding in recorded_findings}))
        reason_code = "PERSONAL_INFORMATION_RECORDED" if recorded_findings else ""
        await run_in_threadpool(audit_upload, audit_filename, "accepted", reason_code, detected, digest, actor)
    except Exception as exc:
        await run_in_threadpool(audit_upload, audit_filename, "failed", "STORAGE_ERROR", "", digest, actor)
        raise HTTPException(status_code=500, detail="검사는 통과했으나 편람 첨부파일 저장에 실패했습니다.") from exc
    return {"message": "편람 자료가 첨부되었습니다.", "attachment": attachment}


@app.delete("/api/admin/handbook-attachments/{attachment_id}")
async def delete_handbook_attachment(
    attachment_id: int,
    _: None = Depends(require_admin),
) -> dict[str, str]:
    removed = await run_in_threadpool(remove_handbook_attachment, attachment_id)
    if not removed:
        raise HTTPException(status_code=404, detail="삭제할 편람 첨부파일을 찾을 수 없습니다.")
    return {"message": "편람 첨부파일을 삭제했습니다."}


@app.get("/api/permissions")
async def list_permissions() -> dict[str, list[dict[str, Any]]]:
    return {"items": [
        {
            "slug": item["slug"],
            "title": item["title"],
            "department": item["department"],
            "page": item["page"],
        }
        for item in PERMISSION_DOCUMENTS
    ]}


@app.get("/api/permissions/{slug}")
async def permission_detail(slug: str) -> dict[str, Any]:
    item = next((item for item in PERMISSION_DOCUMENTS if item["slug"] == slug), None)
    if not item:
        raise HTTPException(status_code=404, detail="권한신청 항목을 찾을 수 없습니다.")
    return item


@app.get("/api/permissions/{slug}/attachments")
async def permission_attachments(slug: str) -> dict[str, list[dict[str, Any]]]:
    if not any(item["slug"] == slug for item in PERMISSION_DOCUMENTS):
        raise HTTPException(status_code=404, detail="권한신청 항목을 찾을 수 없습니다.")
    with db_connect() as connection:
        rows = connection.execute(
            """SELECT id, original_filename, file_type, size_bytes, created_at, created_by
               FROM permission_attachments WHERE permission_slug = ? ORDER BY id DESC""",
            (slug,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/permission-attachments/{attachment_id}/download")
async def download_permission_attachment(attachment_id: int) -> FileResponse:
    with db_connect() as connection:
        row = connection.execute(
            """SELECT original_filename, stored_filename, media_type
               FROM permission_attachments WHERE id = ?""",
            (attachment_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")
    path = PERMISSION_STORAGE_DIR / Path(row["stored_filename"]).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="첨부파일 원본을 찾을 수 없습니다.")
    return FileResponse(path, media_type=row["media_type"], filename=row["original_filename"])


@app.post("/api/admin/permission-attachments")
async def upload_permission_attachment(
    file: Annotated[UploadFile, File(...)],
    permission_slug: Annotated[str, Form(...)],
    admin_name: Annotated[str, Form()] = "담당자",
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    permission = next((item for item in PERMISSION_DOCUMENTS if item["slug"] == permission_slug), None)
    if not permission:
        raise HTTPException(status_code=404, detail="권한신청 항목을 찾을 수 없습니다.")
    if not permission.get("attachments_enabled", True):
        raise HTTPException(status_code=422, detail="이 권한 분류는 한글파일 첨부 대상이 아닙니다.")
    actor = normalize_metadata(admin_name, "담당자명", 50)
    filename = normalize_metadata(Path(file.filename or "").name, "파일명", 255)
    if Path(filename).suffix.lower() not in {".hwp", ".hwpx"}:
        raise HTTPException(status_code=422, detail="권한신청 첨부는 HWP 또는 HWPX 파일만 등록할 수 있습니다.")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    digest = hashlib.sha256(content).hexdigest()
    audit_filename = f"permission-{digest[:12]}{Path(filename).suffix.lower()}"
    metadata_findings = scan_personal_information([TextPart("첨부 정보", f"{filename}\n{actor}")])
    if metadata_findings:
        detected = ", ".join(sorted({finding.kind for finding in metadata_findings}))
        actor_findings = scan_personal_information([TextPart("담당자명", actor)])
        safe_actor = "차단된 입력" if actor_findings else actor
        await run_in_threadpool(
            audit_upload, audit_filename, "blocked", "PERSONAL_INFORMATION_DETECTED", detected, digest, safe_actor,
        )
        raise HTTPException(status_code=422, detail={
            "code": "PERSONAL_INFORMATION_DETECTED",
            "message": "첨부 정보에서 개인정보 패턴이 탐지되어 업로드를 차단했습니다.",
            "findings": [
                {"type": finding.kind, "location": finding.location, "count": finding.count}
                for finding in metadata_findings[:30]
            ],
        })
    try:
        inspection = await run_in_threadpool(inspect_document, filename, content)
    except DocumentRejected as exc:
        detected = ", ".join(sorted({finding.kind for finding in exc.findings}))
        await run_in_threadpool(audit_upload, audit_filename, "blocked", exc.code, detected, digest, actor)
        raise HTTPException(status_code=422, detail={
            "code": exc.code,
            "message": exc.message,
            "findings": [
                {"type": finding.kind, "location": finding.location, "count": finding.count}
                for finding in exc.findings[:30]
            ],
        }) from exc
    if inspection.file_type not in {"hwp", "hwpx"}:
        raise HTTPException(status_code=422, detail="권한신청 첨부는 HWP 또는 HWPX 파일만 등록할 수 있습니다.")
    try:
        attachment = await run_in_threadpool(
            persist_permission_attachment,
            permission_slug=permission_slug,
            content=content,
            inspection=inspection,
            filename=filename,
            actor=actor,
        )
        await run_in_threadpool(audit_upload, audit_filename, "accepted", "", "", digest, actor)
    except Exception as exc:
        await run_in_threadpool(audit_upload, audit_filename, "failed", "STORAGE_ERROR", "", digest, actor)
        raise HTTPException(status_code=500, detail="검사는 통과했으나 첨부파일 저장에 실패했습니다.") from exc
    return {"message": "한글파일이 권한 분류에 첨부되었습니다.", "attachment": attachment}


@app.get("/api/documents", response_model=DocumentList)
async def list_documents(
    q: str | None = Query(None, max_length=100),
    category: str | None = Query(None, max_length=80),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> DocumentList:
    clauses = ["d.status = 'published'"]
    parameters: list[Any] = []
    if category:
        clauses.append("d.category = ?")
        parameters.append(category)
    if q:
        tokens = re.findall(r"[0-9A-Za-z가-힣]+", q)[:8]
        if tokens:
            fts_query = " AND ".join(f'"{token}"*' for token in tokens)
            clauses.append(
                "d.id IN (SELECT CAST(document_id AS INTEGER) FROM document_search WHERE document_search MATCH ?)"
            )
            parameters.append(fts_query)
    where = " AND ".join(clauses)
    offset = (page - 1) * page_size
    with db_connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) FROM documents d JOIN document_versions v ON v.id = d.current_version_id WHERE {where}",
            parameters,
        ).fetchone()[0]
        rows = connection.execute(
            f"{base_document_query()} WHERE {where} ORDER BY d.updated_at DESC, d.id LIMIT ? OFFSET ?",
            [*parameters, page_size, offset],
        ).fetchall()
    return DocumentList(items=[row_to_summary(row) for row in rows], total=total, page=page, page_size=page_size)


@app.get("/api/documents/{identifier}", response_model=DocumentDetail)
async def document_detail(identifier: str) -> DocumentDetail:
    row = get_document_row(identifier)
    with db_connect() as connection:
        headings = connection.execute(
            "SELECT level, title, anchor, position FROM document_headings WHERE version_id = ? ORDER BY position",
            (row["current_version_id"],),
        ).fetchall()
    summary = row_to_summary(row)
    return DocumentDetail(
        **summary.model_dump(), content=row["extracted_text"],
        outline=[OutlineItem(**dict(item)) for item in headings],
        original_filename=f"{row['title']}.{row['file_type']}" if row["stored_filename"] else None,
        file_type=row["file_type"],
        size_bytes=row["size_bytes"], page_count=row["page_count"],
        downloadable=bool(row["stored_filename"]),
    )


@app.get("/api/documents/{identifier}/versions", response_model=list[VersionItem])
async def document_versions(identifier: str) -> list[VersionItem]:
    row = get_document_row(identifier)
    with db_connect() as connection:
        versions = connection.execute(
            """SELECT id, version_no, change_note, original_filename, file_type, size_bytes,
                      page_count, created_at, created_by
               FROM document_versions WHERE document_id = ? ORDER BY version_no DESC""",
            (row["id"],),
        ).fetchall()
    return [VersionItem(
        id=item["id"], version=item["version_no"], change_note=item["change_note"],
        original_filename=f"{row['title']}.{item['file_type']}" if item["original_filename"] else None,
        file_type=item["file_type"],
        size_bytes=item["size_bytes"], page_count=item["page_count"],
        created_at=item["created_at"], created_by=item["created_by"],
    ) for item in versions]


@app.get("/api/documents/{identifier}/download")
async def download_document(identifier: str) -> FileResponse:
    row = get_document_row(identifier)
    if not row["stored_filename"] or not row["original_filename"]:
        raise HTTPException(status_code=404, detail="샘플 문서는 원본 파일을 제공하지 않습니다.")
    path = STORAGE_DIR / Path(row["stored_filename"]).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="원본 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type=row["media_type"], filename=f"{row['title']}.{row['file_type']}")


@app.post("/api/admin/documents", dependencies=[])
async def upload_document(
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str, Form(...)],
    category: Annotated[str, Form(...)],
    department: Annotated[str, Form(...)],
    summary: Annotated[str, Form()] = "",
    change_note: Annotated[str, Form()] = "",
    existing_document_id: Annotated[int | None, Form(ge=1)] = None,
    admin_name: Annotated[str, Form()] = "담당자",
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    title = normalize_metadata(title, "제목", 150)
    category = normalize_metadata(category, "분류", 80)
    department = normalize_metadata(department, "담당부서", 120)
    summary = normalize_metadata(summary, "요약", 500, required=False)
    change_note = normalize_metadata(change_note, "개정사유", 500, required=False)
    actor = normalize_metadata(admin_name, "담당자명", 50)
    if existing_document_id and not change_note:
        raise HTTPException(status_code=422, detail="기존 문서를 개정할 때는 개정사유를 입력하세요.")

    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=422, detail="파일 이름이 필요합니다.")
    filename = normalize_metadata(filename, "파일명", 255)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    digest = hashlib.sha256(content).hexdigest()
    audit_filename = f"upload-{digest[:12]}{Path(filename).suffix.lower() if Path(filename).suffix.lower() in {'.hwp', '.hwpx', '.pdf', '.docx'} else '.bin'}"
    metadata_findings = scan_personal_information([
        TextPart("문서 정보", "\n".join((title, category, department, summary, change_note, filename, actor)))
    ])
    if metadata_findings:
        detected = ", ".join(sorted({finding.kind for finding in metadata_findings}))
        actor_findings = scan_personal_information([TextPart("담당자명", actor)])
        safe_actor = "차단된 입력" if actor_findings else actor
        await run_in_threadpool(audit_upload, audit_filename, "blocked", "PERSONAL_INFORMATION_DETECTED", detected, digest, safe_actor)
        raise HTTPException(status_code=422, detail={
            "code": "PERSONAL_INFORMATION_DETECTED",
            "message": "문서 정보에서 개인정보 패턴이 탐지되어 업로드를 차단했습니다.",
            "findings": [
                {"type": finding.kind, "location": finding.location, "count": finding.count}
                for finding in metadata_findings[:30]
            ],
        })
    try:
        inspection = await run_in_threadpool(inspect_document, filename, content)
    except DocumentRejected as exc:
        detected = ", ".join(sorted({finding.kind for finding in exc.findings}))
        await run_in_threadpool(audit_upload, audit_filename, "blocked", exc.code, detected, digest, actor)
        raise HTTPException(status_code=422, detail={
            "code": exc.code,
            "message": exc.message,
            "findings": [
                {"type": finding.kind, "location": finding.location, "count": finding.count}
                for finding in exc.findings[:30]
            ],
        }) from exc
    except Exception as exc:
        await run_in_threadpool(audit_upload, audit_filename, "failed", "UNREADABLE_DOCUMENT", "", digest, actor)
        raise HTTPException(status_code=422, detail="문서 구조를 안전하게 검사할 수 없어 업로드를 차단했습니다.") from exc

    try:
        document_id, version_id, version_no = await run_in_threadpool(
            persist_document,
            content=content,
            inspection=inspection,
            filename=f"{title}.{inspection.file_type}",
            title=title,
            category=category,
            department=department,
            summary=summary,
            change_note=change_note or "신규 등록",
            existing_document_id=existing_document_id,
            actor=actor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        await run_in_threadpool(audit_upload, audit_filename, "failed", "STORAGE_ERROR", "", digest, actor)
        raise HTTPException(status_code=500, detail="검사는 통과했으나 문서 저장에 실패했습니다.") from exc

    return {
        "message": "개인정보 검사를 통과해 문서가 등록되었습니다.",
        "document_id": document_id,
        "version_id": version_id,
        "version": version_no,
        "file_type": inspection.file_type,
        "characters": len(inspection.text),
        "sections": inspection.page_count,
    }


@app.get("/api/admin/audits", dependencies=[Depends(require_admin)])
async def upload_audits(limit: int = Query(50, ge=1, le=200)) -> dict[str, list[dict[str, Any]]]:
    with db_connect() as connection:
        rows = connection.execute(
            """SELECT id, filename, result, reason_code, detected_types, actor, created_at
               FROM upload_audits ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}

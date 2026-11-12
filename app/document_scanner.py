from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import olefile
from defusedxml import ElementTree
from pypdf import PdfReader

MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5_000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_DOCUMENT_PAGES = 1_000
MAX_EXTRACTED_TEXT_CHARS = 10_000_000
MAX_HWP_SECTION_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".hwp", ".hwpx", ".pdf", ".docx"}

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


@dataclass(frozen=True)
class TextPart:
    location: str
    text: str
    include_in_content: bool = True


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    count: int


@dataclass(frozen=True)
class InspectionResult:
    file_type: str
    media_type: str
    text: str
    page_count: int
    findings: tuple[Finding, ...]


class DocumentRejected(ValueError):
    def __init__(self, code: str, message: str, findings: Iterable[Finding] = ()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.findings = tuple(findings)


def _safe_zip(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise DocumentRejected("INVALID_FILE", "올바른 ZIP 기반 문서가 아닙니다.") from exc

    entries = archive.infolist()
    normalized_names = [entry.filename.replace("\\", "/").lower() for entry in entries]
    if len(normalized_names) != len(set(normalized_names)):
        archive.close()
        raise DocumentRejected("DUPLICATE_ARCHIVE_ENTRY", "중복된 내부 파일명이 있는 문서는 업로드할 수 없습니다.")
    total_size = sum(entry.file_size for entry in entries)
    if len(entries) > MAX_ARCHIVE_ENTRIES or total_size > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise DocumentRejected("ARCHIVE_LIMIT", "압축 해제 크기 또는 파일 수가 허용 범위를 초과합니다.")
    for entry in entries:
        normalized = entry.filename.replace("\\", "/")
        if normalized.startswith("/") or "../" in f"/{normalized}":
            archive.close()
            raise DocumentRejected("INVALID_ARCHIVE_PATH", "압축 파일에 안전하지 않은 경로가 있습니다.")
        if entry.flag_bits & 0x1:
            archive.close()
            raise DocumentRejected("ENCRYPTED_FILE", "암호화된 문서는 검사할 수 없어 업로드할 수 없습니다.")
        if entry.compress_size and entry.file_size / entry.compress_size > 200:
            archive.close()
            raise DocumentRejected("ARCHIVE_LIMIT", "비정상적으로 높은 압축률의 문서는 업로드할 수 없습니다.")
    return archive


def detect_file_type(filename: str, content: bytes) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise DocumentRejected("EXTENSION_NOT_ALLOWED", "HWP, HWPX, PDF, DOCX 파일만 업로드할 수 있습니다.")

    if content.startswith(b"%PDF-"):
        detected = "pdf"
    elif content.startswith(OLE_SIGNATURE):
        detected = "hwp"
    elif content.startswith(ZIP_SIGNATURES):
        with _safe_zip(content) as archive:
            name_map = {name.replace("\\", "/").lower(): name for name in archive.namelist()}
            names = set(name_map)
            if "word/document.xml" in names:
                detected = "docx"
            elif "contents/content.hpf" in names or "mimetype" in names and archive.read(name_map["mimetype"]).strip() == b"application/hwp+zip":
                detected = "hwpx"
            else:
                raise DocumentRejected("UNSUPPORTED_ARCHIVE", "지원하는 HWPX 또는 DOCX 문서 구조가 아닙니다.")
    else:
        raise DocumentRejected("SIGNATURE_MISMATCH", "파일 내용과 허용된 문서 형식이 일치하지 않습니다.")

    if extension != f".{detected}":
        raise DocumentRejected("SIGNATURE_MISMATCH", "파일 확장자와 실제 문서 형식이 일치하지 않습니다.")
    return detected


def _xml_text(payload: bytes, *, include_text: bool = True, include_attributes: bool = False) -> str:
    try:
        root = ElementTree.fromstring(payload)
    except Exception as exc:
        raise DocumentRejected("INVALID_XML", "문서 내부 XML을 안전하게 읽을 수 없습니다.") from exc
    values: list[str] = []

    def walk(element, depth: int = 0) -> None:
        if depth > 200:
            raise DocumentRejected("XML_DEPTH_LIMIT", "문서 내부 XML의 중첩 깊이가 허용 범위를 초과합니다.")
        if include_attributes:
            for attribute in element.attrib.values():
                if attribute and attribute.strip():
                    values.append(attribute)
        if include_text and element.text and element.text.strip():
            values.append(element.text)
        for child in element:
            walk(child, depth + 1)
            if include_text and child.tail and child.tail.strip():
                values.append(child.tail)
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"p", "para", "tr", "table"} and values and values[-1] != "\n":
            values.append("\n")

    walk(root)
    extracted = "".join(values).strip()
    if len(extracted) > MAX_EXTRACTED_TEXT_CHARS:
        raise DocumentRejected("TEXT_LIMIT", "문서 내부 XML 텍스트 크기가 허용 범위를 초과합니다.")
    return extracted


MAX_PDF_ACTION_DEPTH = 200
DANGEROUS_PDF_ACTIONS = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/Rendition", "/GoToE"}


def _resolve_pdf(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _validate_pdf_action(value, location: str, depth: int = 0, seen: set[int] | None = None) -> None:
    """실행 동작(/S가 있는 사전)만 따라가며 위험 동작을 찾는다.

    문서 열기 위치(/OpenAction [페이지 /Fit])처럼 페이지를 가리키는 이동 대상은
    실행 동작이 아니므로 페이지 트리 안으로 들어가지 않는다.
    """
    seen = set() if seen is None else seen
    if depth > MAX_PDF_ACTION_DEPTH:
        raise DocumentRejected("ACTIVE_CONTENT", f"{location} 실행 동작의 구조가 너무 복잡합니다.")
    try:
        action = _resolve_pdf(value)
    except Exception as exc:
        raise DocumentRejected("UNINSPECTABLE_PDF", f"{location} 실행 동작을 검사할 수 없습니다.") from exc
    if id(action) in seen:
        return
    seen.add(id(action))
    if isinstance(action, (list, tuple)):
        # 동작 배열(/Next) 또는 이동 대상 배열([페이지 /XYZ ...]). 동작 사전만 검사한다.
        for item in action:
            try:
                resolved = _resolve_pdf(item)
            except Exception as exc:
                raise DocumentRejected("UNINSPECTABLE_PDF", f"{location} 실행 동작을 검사할 수 없습니다.") from exc
            if hasattr(resolved, "get") and resolved.get("/S") is not None:
                _validate_pdf_action(resolved, location, depth + 1, seen)
        return
    if not hasattr(action, "get"):
        return
    action_type = str(action.get("/S", ""))
    if not action_type:
        # 추가 동작 사전(/AA)의 값 중 실제 동작만 검사하고, 페이지 등 다른 객체는 따라가지 않는다.
        for nested in action.values():
            try:
                resolved = _resolve_pdf(nested)
            except Exception as exc:
                raise DocumentRejected("UNINSPECTABLE_PDF", f"{location} 실행 동작을 검사할 수 없습니다.") from exc
            if hasattr(resolved, "get") and resolved.get("/S") is not None:
                _validate_pdf_action(resolved, location, depth + 1, seen)
        return
    if action.get("/JS") is not None or action_type in DANGEROUS_PDF_ACTIONS:
        raise DocumentRejected("ACTIVE_CONTENT", f"{location}에 스크립트 또는 외부 실행 동작이 포함되어 있습니다.")
    for key in ("/Next", "/A", "/AA"):
        nested = action.get(key)
        if nested is not None:
            _validate_pdf_action(nested, location, depth + 1, seen)


def _extract_pdf(content: bytes, *, allow_images: bool = False, allow_actions: bool = False) -> list[TextPart]:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
    except Exception as exc:
        raise DocumentRejected("INVALID_PDF", "올바른 PDF 파일이 아닙니다.") from exc
    if reader.is_encrypted:
        raise DocumentRejected("ENCRYPTED_FILE", "암호화된 PDF는 검사할 수 없어 업로드할 수 없습니다.")
    if not reader.pages or len(reader.pages) > MAX_DOCUMENT_PAGES:
        raise DocumentRejected("PAGE_LIMIT", "PDF 페이지 수가 허용 범위를 벗어났습니다.")

    try:
        if reader.attachments:
            raise DocumentRejected("EMBEDDED_OBJECT", "첨부파일이 포함된 PDF는 전체 검사할 수 없어 업로드할 수 없습니다.")
    except AttributeError:
        pass
    root_keys = {str(key) for key in reader.root_object.keys()}
    if "/JavaScript" in root_keys:
        raise DocumentRejected("ACTIVE_CONTENT", "자동 실행 또는 스크립트가 포함된 PDF는 업로드할 수 없습니다.")
    root_actions = root_keys.intersection({"/OpenAction", "/AA"})
    if root_actions and not allow_actions:
        raise DocumentRejected("ACTIVE_CONTENT", "자동 실행 동작이 포함된 PDF는 업로드할 수 없습니다.")
    if allow_actions:
        for key in root_actions:
            _validate_pdf_action(reader.root_object.get(key), "PDF 문서")
    try:
        names_object = reader.root_object.get("/Names")
        if names_object:
            names_keys = {str(key) for key in names_object.get_object().keys()}
            if names_keys.intersection({"/EmbeddedFiles", "/JavaScript"}):
                raise DocumentRejected("ACTIVE_CONTENT", "첨부파일 또는 스크립트가 포함된 PDF는 업로드할 수 없습니다.")
    except DocumentRejected:
        raise
    except Exception as exc:
        raise DocumentRejected("UNINSPECTABLE_PDF", "PDF 이름 트리를 안전하게 검사할 수 없습니다.") from exc

    parts: list[TextPart] = []
    unreadable: list[int] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise DocumentRejected("TEXT_EXTRACTION_FAILED", f"PDF {number}쪽 본문을 검사할 수 없습니다.") from exc
        meaningful_count = len(re.findall(r"[0-9A-Za-z가-힣]", text))
        has_images = False
        try:
            has_images = bool(page.images)
        except Exception:
            has_images = "/XObject" in page.get("/Resources", {})
        if has_images and not allow_images:
            raise DocumentRejected(
                "UNINSPECTABLE_IMAGE",
                f"PDF {number}쪽에 이미지가 있어 내부 개인정보를 전수검사할 수 없습니다. 이미지가 없는 텍스트 PDF를 업로드하세요.",
            )
        if not meaningful_count and not allow_images:
            unreadable.append(number)
        parts.append(TextPart(f"{number}쪽", text))
        continuous_text = text.replace("\r", "").replace("\n", "")
        if continuous_text != text:
            parts.append(TextPart(f"{number}쪽 연속텍스트", continuous_text, False))
        if sum(len(part.text) for part in parts) > MAX_EXTRACTED_TEXT_CHARS:
            raise DocumentRejected("TEXT_LIMIT", "추출된 PDF 본문 크기가 허용 범위를 초과합니다.")

        annotations = page.get("/Annots") or []
        for annotation_index, annotation_ref in enumerate(annotations, start=1):
            try:
                annotation = annotation_ref.get_object()
                annotation_text = "\n".join(
                    str(annotation.get(key, "")) for key in ("/Contents", "/T", "/V", "/TU")
                )
                if annotation_text.strip():
                    parts.append(TextPart(f"{number}쪽 주석 {annotation_index}", annotation_text, False))
                if (annotation.get("/A") or annotation.get("/AA")) and not allow_actions:
                    raise DocumentRejected("ACTIVE_CONTENT", f"PDF {number}쪽에 실행 동작이 포함되어 있습니다.")
                if allow_actions:
                    for key in ("/A", "/AA"):
                        if annotation.get(key) is not None:
                            _validate_pdf_action(annotation.get(key), f"PDF {number}쪽 주석 {annotation_index}")
            except DocumentRejected:
                raise
            except Exception as exc:
                if allow_actions:
                    continue
                raise DocumentRejected("UNINSPECTABLE_ANNOTATION", f"PDF {number}쪽 주석을 검사할 수 없습니다.") from exc

    if unreadable:
        shown = ", ".join(map(str, unreadable[:10]))
        raise DocumentRejected(
            "UNINSPECTABLE_PAGE",
            f"텍스트를 검사할 수 없는 PDF 페이지가 있습니다({shown}쪽). 이미지 없는 텍스트 PDF로 다시 업로드하세요.",
        )
    metadata = "\n".join(
        str(value)
        for key, value in (reader.metadata or {}).items()
        if value and str(key) not in {"/CreationDate", "/ModDate"}
    )
    if metadata:
        parts.append(TextPart("문서 속성", metadata, False))
    try:
        fields = reader.get_fields() or {}
        field_text = "\n".join(
            str(value)
            for field in fields.values()
            for value in (field.get("/T"), field.get("/V"), field.get("/DV"), field.get("/TU"))
            if value
        )
        if field_text:
            parts.append(TextPart("양식 필드", field_text, False))
    except Exception as exc:
        if not allow_actions:
            raise DocumentRejected("UNINSPECTABLE_PDF", "PDF 양식 필드를 안전하게 검사할 수 없습니다.") from exc
    return parts


def _extract_docx(content: bytes, *, allow_images: bool = False) -> list[TextPart]:
    with _safe_zip(content) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
        lowered = {name.lower(): name for name in names}
        if any(name.startswith("word/embeddings/") for name in lowered):
            raise DocumentRejected("EMBEDDED_OBJECT", "내장 파일이 포함된 DOCX는 전체 검사할 수 없어 업로드할 수 없습니다.")
        if "word/vbaproject.bin" in lowered:
            raise DocumentRejected("MACRO_NOT_ALLOWED", "매크로가 포함된 문서는 업로드할 수 없습니다.")
        if any(name.startswith("word/activex/") for name in lowered):
            raise DocumentRejected("ACTIVE_CONTENT", "ActiveX가 포함된 DOCX는 업로드할 수 없습니다.")
        if any(name.startswith("word/media/") for name in lowered) and not allow_images:
            raise DocumentRejected(
                "UNINSPECTABLE_IMAGE",
                "이미지가 포함된 DOCX는 이미지 내부를 전수검사할 수 없습니다. 이미지를 제거한 텍스트 문서를 업로드하세요.",
            )
        unsupported_binary = [
            name for name in names
            if not name.endswith("/") and not name.lower().endswith((".xml", ".rels"))
            and not (allow_images and name.lower().startswith("word/media/") and Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".wmf", ".emf"})
        ]
        if unsupported_binary:
            raise DocumentRejected("UNINSPECTABLE_BINARY", "검사할 수 없는 바이너리 항목이 포함된 DOCX는 업로드할 수 없습니다.")

        relationship_names = [name for name in names if name.lower().endswith(".rels")]
        for name in relationship_names:
            relationship_payload = archive.read(name)
            if re.search(rb"TargetMode\s*=\s*['\"]External|attachedTemplate", relationship_payload, re.IGNORECASE):
                raise DocumentRejected("EXTERNAL_CONTENT", "외부 연결이 포함된 DOCX는 업로드할 수 없습니다.")

        wanted = [
            name for name in names
            if name.lower() == "word/document.xml"
            or re.fullmatch(r"word/(header|footer)\d+\.xml", name.lower())
            or name.lower() in {
                "word/comments.xml", "word/footnotes.xml", "word/endnotes.xml",
                "docprops/core.xml", "docprops/custom.xml",
            }
        ]
        scan_names = [name for name in names if name.lower().endswith((".xml", ".rels"))]
        parts: list[TextPart] = []
        total_text_size = 0
        for name in scan_names:
            payload = archive.read(name)
            display_text = _xml_text(payload)
            parts.append(TextPart(
                name,
                display_text,
                name in wanted and not name.lower().startswith("docprops/"),
            ))
            continuous_text = display_text.replace("\n", "")
            if continuous_text != display_text:
                parts.append(TextPart(f"{name} 연속텍스트", continuous_text, False))
            attributes = _xml_text(payload, include_text=False, include_attributes=True)
            if attributes:
                parts.append(TextPart(f"{name} 속성", attributes, False))
            total_text_size += len(display_text) + len(attributes)
            if total_text_size > MAX_EXTRACTED_TEXT_CHARS:
                raise DocumentRejected("TEXT_LIMIT", "추출된 DOCX 텍스트 크기가 허용 범위를 초과합니다.")
    body = next((part.text for part in parts if part.location.lower() == "word/document.xml"), "")
    if not re.search(r"[0-9A-Za-z가-힣]", body):
        raise DocumentRejected("EMPTY_DOCUMENT", "검사할 수 있는 DOCX 본문이 없습니다.")
    return parts


def _extract_hwpx(content: bytes, *, allow_images: bool = False) -> list[TextPart]:
    with _safe_zip(content) as archive:
        names = [name.replace("\\", "/") for name in archive.namelist()]
        lowered = {name.lower(): name for name in names}
        if any("ole" in name or "scripts/" in name for name in lowered):
            raise DocumentRejected("EMBEDDED_OBJECT", "스크립트 또는 내장 객체가 포함된 HWPX는 업로드할 수 없습니다.")
        if any(name.startswith("bindata/") for name in lowered) and not allow_images:
            raise DocumentRejected(
                "UNINSPECTABLE_IMAGE",
                "이미지가 포함된 HWPX는 이미지 내부를 전수검사할 수 없습니다. 이미지를 제거한 텍스트 문서를 업로드하세요.",
            )
        standard_preview_files = {
            "preview/prvtext.txt",
            "preview/prvimage.png",
            "meta-inf/container.rdf",
        }
        unsupported_binary = [
            name for name in names
            if not name.endswith("/")
            and name.lower() != "mimetype"
            and not name.lower().endswith((".xml", ".hpf"))
            and name.lower() not in standard_preview_files
            and not (allow_images and name.lower().startswith("bindata/") and Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".wmf", ".emf"})
        ]
        if unsupported_binary:
            raise DocumentRejected("UNINSPECTABLE_BINARY", "검사할 수 없는 바이너리 항목이 포함된 HWPX는 업로드할 수 없습니다.")
        section_names = sorted(
            (name for name in names if re.fullmatch(r"Contents/section\d+\.xml", name, re.IGNORECASE)),
            key=lambda value: int(re.search(r"\d+", value).group()),
        )
        if not section_names:
            raise DocumentRejected("INVALID_HWPX", "HWPX 본문 구조를 찾을 수 없습니다.")
        wanted = section_names + [
            name for name in names
            if name.lower() in {"contents/header.xml", "contents/content.hpf", "settings.xml", "version.xml"}
        ]
        scan_names = [name for name in names if name.lower().endswith((".xml", ".hpf"))]
        parts: list[TextPart] = []
        total_text_size = 0
        for name in scan_names:
            payload = archive.read(name)
            location = f"구역 {section_names.index(name) + 1}" if name in section_names else name
            display_text = _xml_text(payload)
            parts.append(TextPart(location, display_text, name in section_names))
            continuous_text = display_text.replace("\n", "")
            if continuous_text != display_text:
                parts.append(TextPart(f"{location} 연속텍스트", continuous_text, False))
            total_text_size += len(display_text)
            if total_text_size > MAX_EXTRACTED_TEXT_CHARS:
                raise DocumentRejected("TEXT_LIMIT", "추출된 HWPX 텍스트 크기가 허용 범위를 초과합니다.")
    body = "\n".join(part.text for part in parts if part.include_in_content)
    if not re.search(r"[0-9A-Za-z가-힣]", body):
        raise DocumentRejected("EMPTY_DOCUMENT", "검사할 수 있는 HWPX 본문이 없습니다.")
    return parts


def _hwp_records(payload: bytes) -> str:
    offset = 0
    values: list[str] = []
    while offset + 4 <= len(payload):
        header = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        tag_id = header & 0x3FF
        size = header >> 20
        if size == 0xFFF:
            if offset + 4 > len(payload):
                raise DocumentRejected("INVALID_HWP", "잘린 HWP 확장 레코드가 있습니다.")
            size = struct.unpack_from("<I", payload, offset)[0]
            offset += 4
        if size < 0 or offset + size > len(payload):
            raise DocumentRejected("INVALID_HWP", "HWP 레코드 크기가 올바르지 않습니다.")
        record = payload[offset:offset + size]
        offset += size
        if tag_id != 67 or not record:
            continue
        text = record.decode("utf-16le", errors="ignore")
        text = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            values.append(text)
    return "\n".join(values)


def _extract_hwp(content: bytes, *, allow_images: bool = False) -> list[TextPart]:
    try:
        document = olefile.OleFileIO(io.BytesIO(content))
    except Exception as exc:
        raise DocumentRejected("INVALID_HWP", "올바른 HWP 파일이 아닙니다.") from exc
    try:
        if not document.exists("FileHeader"):
            raise DocumentRejected("INVALID_HWP", "HWP 파일 헤더를 찾을 수 없습니다.")
        header = document.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise DocumentRejected("INVALID_HWP", "HWP 5.x 문서 형식만 지원합니다.")
        compressed = len(header) >= 40 and bool(struct.unpack_from("<I", header, 36)[0] & 0x1)
        encrypted = len(header) >= 40 and bool(struct.unpack_from("<I", header, 36)[0] & 0x2)
        if encrypted:
            raise DocumentRejected("ENCRYPTED_FILE", "암호화된 HWP는 검사할 수 없어 업로드할 수 없습니다.")
        if document.exists("Scripts"):
            raise DocumentRejected("SCRIPT_NOT_ALLOWED", "스크립트가 포함된 HWP는 업로드할 수 없습니다.")
        if document.exists("PrvImage") and not allow_images:
            raise DocumentRejected(
                "UNINSPECTABLE_IMAGE",
                "미리보기 이미지가 포함된 HWP는 이미지 내부를 전수검사할 수 없습니다. 미리보기와 이미지를 제거한 문서를 업로드하세요.",
            )
        bindata_paths = [path for path in document.listdir() if path and path[0] == "BinData"]
        image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".wmf", ".emf"}
        if bindata_paths and not allow_images:
            raise DocumentRejected(
                "UNINSPECTABLE_IMAGE",
                "이미지 또는 내장 객체가 포함된 HWP는 내부를 전수검사할 수 없습니다. 이미지를 제거한 텍스트 문서를 업로드하세요.",
            )
        if allow_images and any(Path(path[-1]).suffix.lower() not in image_extensions for path in bindata_paths):
            raise DocumentRejected("EMBEDDED_OBJECT", "이미지가 아닌 내장 객체가 포함된 HWP는 업로드할 수 없습니다.")

        section_paths = sorted(
            (path for path in document.listdir() if len(path) == 2 and path[0] == "BodyText" and re.fullmatch(r"Section\d+", path[1])),
            key=lambda path: int(re.search(r"\d+", path[1]).group()),
        )
        if not section_paths:
            raise DocumentRejected("INVALID_HWP", "HWP 본문 구역을 찾을 수 없습니다.")
        parts: list[TextPart] = []
        total_hwp_bytes = 0
        for index, path in enumerate(section_paths, start=1):
            payload = document.openstream(path).read()
            if compressed:
                try:
                    decompressor = zlib.decompressobj(-15)
                    payload = decompressor.decompress(payload, MAX_HWP_SECTION_BYTES + 1)
                    if len(payload) > MAX_HWP_SECTION_BYTES or decompressor.unconsumed_tail:
                        raise DocumentRejected("TEXT_LIMIT", f"HWP 구역 {index}의 압축 해제 크기가 허용 범위를 초과합니다.")
                    payload += decompressor.flush()
                    if not decompressor.eof or decompressor.unused_data:
                        raise DocumentRejected("INVALID_HWP", f"HWP 구역 {index}의 압축 스트림이 올바르지 않습니다.")
                except zlib.error as exc:
                    raise DocumentRejected("INVALID_HWP", f"HWP 구역 {index}의 압축을 해제할 수 없습니다.") from exc
            if len(payload) > MAX_HWP_SECTION_BYTES:
                raise DocumentRejected("TEXT_LIMIT", f"HWP 구역 {index}의 크기가 허용 범위를 초과합니다.")
            total_hwp_bytes += len(payload)
            if total_hwp_bytes > MAX_UNCOMPRESSED_BYTES:
                raise DocumentRejected("TEXT_LIMIT", "HWP 전체 압축 해제 크기가 허용 범위를 초과합니다.")
            section_text = _hwp_records(payload)
            parts.append(TextPart(f"구역 {index}", section_text))
            compact_text = section_text.replace("\n", "")
            if compact_text != section_text:
                parts.append(TextPart(f"구역 {index} 연속텍스트", compact_text, False))
        if document.exists("PrvText"):
            preview = document.openstream("PrvText").read().decode("utf-16le", errors="ignore")
            if preview.strip():
                parts.append(TextPart("미리보기 텍스트", preview, False))
        summary_paths = [path for path in document.listdir() if path and "summaryinformation" in path[-1].lower()]
        for path in summary_paths:
            try:
                properties = document.getproperties(path)
                summary_text = "\n".join(str(value) for value in properties.values() if value)
            except Exception as exc:
                raise DocumentRejected("UNINSPECTABLE_METADATA", "HWP 문서 속성을 안전하게 검사할 수 없습니다.") from exc
            if summary_text:
                parts.append(TextPart("문서 속성", summary_text, False))
    finally:
        document.close()
    if not re.search(r"[0-9A-Za-z가-힣]", "\n".join(part.text for part in parts if part.include_in_content)):
        raise DocumentRejected("EMPTY_DOCUMENT", "검사할 수 있는 HWP 본문이 없습니다.")
    return parts


MOBILE_PHONE = re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
LANDLINE_PHONE = re.compile(r"(?<!\d)0(?:2|[3-6][1-5]|70|80|50\d)[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"(?<![\w.])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.])")
RESIDENT_ID = re.compile(r"(?<!\d)(\d{6})[-\s]?([1-8]\d{6})(?!\d)")
DRIVER_LICENSE = re.compile(r"(?<!\d)\d{2}[-\s]\d{2}[-\s]\d{6}[-\s]\d{2}(?!\d)")
PASSPORT = re.compile(r"(?<![A-Z0-9])(?:[A-Z]{1,2}\d{7,8})(?![A-Z0-9])", re.IGNORECASE)
ACCOUNT = re.compile(r"(?:계좌(?:번호)?|account)\s*[:：]?\s*\d{2,6}(?:[-\s]\d{2,6}){2,5}", re.IGNORECASE)
LONG_NUMBER = re.compile(r"(?<!\d)\d{11,14}(?!\d)")
CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
ADDRESS = re.compile(
    r"(?:(?:[가-힣]+(?:특별자치시|특별자치도|특별시|광역시|도))\s+)?"
    r"(?:(?:[가-힣]+(?:시|군|구))\s+)?"
    r"(?:[가-힣0-9·]+(?:로|길|동|읍|면|리))\s+\d{1,4}(?:-\d{1,4})?"
)


def _valid_birth_date(value: str) -> bool:
    try:
        datetime.strptime(f"20{value}", "%Y%m%d")
        return True
    except ValueError:
        return False


def _luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def scan_personal_information(parts: Iterable[TextPart]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for part in parts:
        checks = {
            "휴대전화번호": len(MOBILE_PHONE.findall(part.text)),
            "전화번호": len(LANDLINE_PHONE.findall(part.text)),
            "이메일 주소": len(EMAIL.findall(part.text)),
            "운전면허번호": len(DRIVER_LICENSE.findall(part.text)),
            "여권번호": len(PASSPORT.findall(part.text)),
            "계좌번호": len(ACCOUNT.findall(part.text)),
            "장문 숫자 식별정보": len(LONG_NUMBER.findall(part.text)),
            "주민·외국인등록번호": sum(
                1 for match in RESIDENT_ID.finditer(part.text) if _valid_birth_date(match.group(1))
            ),
            "신용카드번호": sum(1 for match in CARD_CANDIDATE.finditer(part.text) if _luhn(match.group())),
            "주소": len(ADDRESS.findall(part.text)),
        }
        findings.extend(Finding(kind, part.location, count) for kind, count in checks.items() if count)
    return tuple(findings)


def inspect_document(
    filename: str,
    content: bytes,
    *,
    allow_pdf_images: bool = False,
    allow_pdf_actions: bool = False,
    allow_document_images: bool = False,
    blocked_finding_kinds: frozenset[str] | None = None,
    allow_blocked_findings: bool = False,
) -> InspectionResult:
    if not content:
        raise DocumentRejected("EMPTY_FILE", "빈 파일은 업로드할 수 없습니다.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DocumentRejected("FILE_TOO_LARGE", "파일 크기는 60MB를 초과할 수 없습니다.")

    file_type = detect_file_type(filename, content)
    if file_type == "pdf":
        parts = _extract_pdf(content, allow_images=allow_pdf_images, allow_actions=allow_pdf_actions)
    else:
        extractor = {
            "docx": _extract_docx,
            "hwpx": _extract_hwpx,
            "hwp": _extract_hwp,
        }[file_type]
        parts = extractor(content, allow_images=allow_document_images)
    findings = scan_personal_information(parts)
    blocked_findings = tuple(
        finding for finding in findings
        if blocked_finding_kinds is None or finding.kind in blocked_finding_kinds
    )
    if blocked_findings and not allow_blocked_findings:
        raise DocumentRejected(
            "PERSONAL_INFORMATION_DETECTED",
            "개인정보 패턴이 탐지되어 업로드를 차단했습니다.",
            blocked_findings,
        )

    body_parts = [part for part in parts if part.include_in_content]
    text = "\n\n".join(part.text.strip() for part in body_parts if part.text.strip())
    if len(text) > MAX_EXTRACTED_TEXT_CHARS:
        raise DocumentRejected("TEXT_LIMIT", "추출된 문서 본문 크기가 허용 범위를 초과합니다.")
    media_types = {
        "pdf": "application/pdf",
        "hwp": "application/x-hwp",
        "hwpx": "application/hwp+zip",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return InspectionResult(file_type, media_types[file_type], text, len(body_parts), blocked_findings)

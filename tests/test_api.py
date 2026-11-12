import io
import os
import zipfile
from base64 import b64encode

os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["LOAD_SAMPLE_DATA"] = "false"

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject

from app.document_scanner import DocumentRejected, inspect_document
from app.main import app, rewrite_law_content


def docx_bytes(text: str, extra_files: dict[str, bytes] | None = None) -> bytes:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body>
    </w:document>""".encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("[Content_Types].xml", b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        for name, payload in (extra_files or {}).items():
            archive.writestr(name, payload)
    return output.getvalue()


def docx_runs_bytes(parts: list[str]) -> bytes:
    runs = "".join(f"<w:r><w:t>{part}</w:t></w:r>" for part in parts)
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p>{runs}</w:p></w:body>
    </w:document>""".encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("[Content_Types].xml", b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
    return output.getvalue()


def hwpx_bytes(text: str, standard_preview: bool = False, image: bool = False) -> bytes:
    section = f"<hs:sec xmlns:hs='urn:hancom:section'><hs:p><hs:t>{text}</hs:t></hs:p></hs:sec>".encode()
    content = b"<opf:package xmlns:opf='urn:hancom:opf'><opf:metadata/></opf:package>"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("Contents/content.hpf", content)
        archive.writestr("Contents/section0.xml", section)
        if standard_preview:
            archive.writestr("Preview/PrvText.txt", text.encode())
            archive.writestr("Preview/PrvImage.png", b"preview")
            archive.writestr("META-INF/container.rdf", b"<rdf:RDF xmlns:rdf='urn:rdf'/>")
        if image:
            archive.writestr("BinData/image.png", b"\x89PNG\r\n\x1a\n")
    return output.getvalue()


def image_pdf_bytes() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    image = DecodedStreamObject()
    image.set_data(b"\x00\x80\xff")
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1),
        NameObject("/Height"): NumberObject(1),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    image_ref = writer._add_object(image)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref}),
    })
    content = DecodedStreamObject()
    content.set_data(b"q 80 0 0 80 10 10 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def action_pdf_bytes() -> bytes:
    reader = PdfReader(io.BytesIO(image_pdf_bytes()))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.add_uri(0, "https://www.law.go.kr/", RectangleObject((10, 10, 90, 30)))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def configure_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.main.STORAGE_DIR", tmp_path / "documents")
    monkeypatch.setattr("app.main.PERMISSION_STORAGE_DIR", tmp_path / "permission_attachments")
    monkeypatch.setattr("app.main.HANDBOOK_STORAGE_DIR", tmp_path / "handbook_attachments")
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "manuals.db")


def upload(client: TestClient, content: bytes, **overrides):
    data = {
        "title": "전입신고 업무",
        "category": "주민등록시스템",
        "department": "마을자치과",
        "summary": "전입신고 처리 절차",
        "admin_name": "테스트 담당자",
        **overrides,
    }
    return client.post(
        "/api/admin/documents",
        headers={"X-Admin-Key": "test-admin-key"},
        data=data,
        files={"file": ("manual.docx", content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )


def test_health_and_empty_document_list(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        home = client.get("/")
        assert "통합민원대 업무지원서비스" in home.text
        assert "PDF 원문 서식" in home.text
        assert "권한신청서식" in home.text
        assert "현재버전" not in home.text
        assert "개정이력" not in home.text
        assert 'id="contentPanel"' not in home.text
        assert "매뉴얼 자료 등록" in client.get("/admin").text
        assert home.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/documents").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_law_proxy_rewrites_same_site_paths():
    source = '<script src="/LSW/app.js"></script><script>location.href="/LSW/search.do"</script>'
    rewritten = rewrite_law_content(source, "text/html")
    assert 'src="/law-proxy/LSW/app.js"' in rewritten
    assert 'location.href="/law-proxy/LSW/search.do"' in rewritten


def test_admin_auth_accepts_utf8_base64_key(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("ADMIN_KEY", "동구062-608")
    encoded = b64encode("동구062-608".encode()).decode()
    with TestClient(app) as client:
        assert client.get("/api/admin/audits", headers={"X-Admin-Key": f"b64:{encoded}"}).status_code == 200
        assert client.get("/api/admin/audits", headers={"X-Admin-Key": "062608"}).status_code == 401


def test_related_sites_are_seeded_and_admin_can_add_site(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        initial = client.get("/api/related-sites").json()["items"]
        assert [item["name"] for item in initial] == [
            "국가법령정보센터",
            "나의 사건 검색",
            "웹팩스",
            "모바일 신분증 진위확인·사본저장 시스템",
        ]
        response = client.post(
            "/api/admin/related-sites",
            headers={"X-Admin-Key": "test-admin-key"},
            data={
                "name": "정부24",
                "url": "https://www.gov.kr/",
                "admin_name": "테스트 담당자",
            },
        )
        assert response.status_code == 200
        updated = client.get("/api/related-sites").json()["items"]
        assert updated[-1]["name"] == "정부24"
        assert updated[-1]["url"] == "https://www.gov.kr/"


def test_source_manual_is_served_inline(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr("app.main.SOURCE_MANUAL_PATH", source)
    with TestClient(app) as client:
        response = client.get("/source-manual.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")


def test_source_manual_serves_requested_page_range(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=595, height=842)
    with source.open("wb") as output:
        writer.write(output)
    monkeypatch.setattr("app.main.SOURCE_MANUAL_PATH", source)
    with TestClient(app) as client:
        response = client.get("/source-manual.pdf", params={"start": 2, "end": 3})
        assert response.status_code == 200
        assert len(PdfReader(io.BytesIO(response.content)).pages) == 2


def test_sample_data_initializes_twelve_categories(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("LOAD_SAMPLE_DATA", "true")
    with TestClient(app) as client:
        categories = client.get("/api/categories").json()["items"]
        documents = client.get("/api/documents", params={"page_size": 100}).json()
        assert len(categories) == 12
        assert documents["total"] == 12


def test_permission_guide_is_grouped_by_system(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        permissions = client.get("/api/permissions").json()["items"]
        assert len(permissions) == 16
        assert permissions[0]["title"] == "인증서발급"
        detail = client.get("/api/permissions/resident-registration").json()
        assert detail["title"] == "주민등록시스템"
        assert detail["department"] == "마을자치과"
        assert detail["method"] == "① 시스템 사용자 등록\n② ‘주민등록 진위확인 시스템 사용자 등록/삭제 신청서’ 작성 후 공문 발송"
        civil_window = client.get("/api/permissions/civil-service-window").json()
        assert civil_window["method"].endswith("서식 작성 후 교육수료증과 함께 공문발송")
        certificate = client.get("/api/permissions/certificate").json()
        assert certificate["attachments_enabled"] is False


def test_permission_category_accepts_safe_hwpx_attachment(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = hwpx_bytes("주민등록시스템 권한 신청 서식")
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/permission-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"permission_slug": "resident-registration", "admin_name": "테스트 담당자"},
            files={"file": ("권한신청서.hwpx", content, "application/hwp+zip")},
        )
        assert response.status_code == 200
        attachment_id = response.json()["attachment"]["id"]
        attachments = client.get("/api/permissions/resident-registration/attachments").json()["items"]
        assert attachments[0]["original_filename"] == "권한신청서.hwpx"
        download = client.get(f"/api/permission-attachments/{attachment_id}/download")
        assert download.status_code == 200
        assert download.content == content


def test_permission_category_rejects_attachment_when_disabled(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/permission-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"permission_slug": "certificate", "admin_name": "테스트 담당자"},
            files={"file": ("권한신청서.hwpx", hwpx_bytes("인증서 신청 서식"), "application/hwp+zip")},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "이 권한 분류는 한글파일 첨부 대상이 아닙니다."


def test_handbook_categories_are_ordered(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        items = client.get("/api/handbooks").json()["items"]
        assert [item["title"] for item in items] == [
            "출생·사망",
            "안심상속 원스톱서비스 재산조회",
            "가족관계",
            "주민등록",
            "인감·본인서명",
            "외국인정보공동이용시스템",
            "부동산거래관리시스템(임대차신고·확정일자)",
            "민원처리운영창구",
            "KLIP(토지이용계획확인서)",
        ]


def test_handbook_attachment_can_be_uploaded_downloaded_and_deleted(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = hwpx_bytes("출생 사망 업무편람")
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("출생사망편람.hwpx", content, "application/hwp+zip")},
        )
        assert response.status_code == 200
        attachment_id = response.json()["attachment"]["id"]
        detail = client.get("/api/handbooks/birth-death").json()
        assert detail["attachments"][0]["original_filename"] == "출생사망편람.hwpx"
        download = client.get(f"/api/handbook-attachments/{attachment_id}/download")
        assert download.content == content

        removed = client.delete(
            f"/api/admin/handbook-attachments/{attachment_id}",
            headers={"X-Admin-Key": "test-admin-key"},
        )
        assert removed.status_code == 200
        assert client.get("/api/handbooks/birth-death").json()["attachments"] == []
        assert client.get(f"/api/handbook-attachments/{attachment_id}/download").status_code == 404


def test_handbook_allows_document_images_and_non_blocking_personal_patterns(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    documents = [
        ("이미지편람.hwpx", hwpx_bytes("담당 메일 test@example.com 여권 M12345678", image=True), "application/hwp+zip"),
        ("이미지편람.docx", docx_bytes("담당 메일 test@example.com", {"word/media/image1.png": b"\x89PNG\r\n\x1a\n"}), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ]
    with TestClient(app) as client:
        for filename, content, media_type in documents:
            response = client.post(
                "/api/admin/handbook-attachments",
                headers={"X-Admin-Key": "test-admin-key"},
                data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
                files={"file": (filename, content, media_type)},
            )
            assert response.status_code == 200


def test_handbook_allows_personal_information_patterns_and_audits_types(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = hwpx_bytes(
        "담당자 휴대전화 010-2345-6789 주민번호 900101-1234567 주소 광주광역시 동구 금남로 123"
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("필수편람.hwpx", content, "application/hwp+zip")},
        )
        assert response.status_code == 200
        audits = client.get(
            "/api/admin/audits",
            headers={"X-Admin-Key": "test-admin-key"},
        ).json()["items"]
        assert audits[0]["result"] == "accepted"
        assert audits[0]["reason_code"] == "PERSONAL_INFORMATION_RECORDED"
        assert set(audits[0]["detected_types"].split(", ")) == {
            "주민·외국인등록번호",
            "주소",
            "휴대전화번호",
        }


def test_handbook_allows_personal_information_pattern_in_filename(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("010-2345-6789.hwpx", hwpx_bytes("정상 본문"), "application/hwp+zip")},
        )
        assert response.status_code == 200


def test_handbook_allows_landline_phone(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("유선전화.hwpx", hwpx_bytes("대표전화 062-608-2114"), "application/hwp+zip")},
        )
        assert response.status_code == 200


def test_handbook_accepts_image_pdf_and_serves_it_inline(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = image_pdf_bytes()
    try:
        inspect_document("편람.pdf", content)
        raise AssertionError("일반 문서 업로드가 이미지 PDF를 허용함")
    except DocumentRejected as exc:
        assert exc.code == "UNINSPECTABLE_IMAGE"

    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("이미지편람.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200
        attachment_id = response.json()["attachment"]["id"]
        view = client.get(f"/api/handbook-attachments/{attachment_id}/view")
        assert view.status_code == 200
        assert view.headers["content-type"] == "application/pdf"
        assert view.headers["content-disposition"].startswith("inline;")
        assert view.content == content


def test_handbook_accepts_pdf_pages_without_extractable_text(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = blank_pdf_bytes()
    try:
        inspect_document("텍스트없는편람.pdf", content)
        raise AssertionError("일반 문서 업로드가 텍스트 없는 PDF를 허용함")
    except DocumentRejected as exc:
        assert exc.code == "UNINSPECTABLE_PAGE"

    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("텍스트없는편람.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200


def test_handbook_accepts_safe_pdf_link_action(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = action_pdf_bytes()
    try:
        inspect_document("실행동작편람.pdf", content, allow_pdf_images=True)
        raise AssertionError("일반 문서 업로드가 실행동작 PDF를 허용함")
    except DocumentRejected as exc:
        assert exc.code == "ACTIVE_CONTENT"

    with TestClient(app) as client:
        response = client.post(
            "/api/admin/handbook-attachments",
            headers={"X-Admin-Key": "test-admin-key"},
            data={"category_slug": "birth-death", "admin_name": "테스트 담당자"},
            files={"file": ("실행동작편람.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200


def test_inspection_accepts_normal_docx():
    result = inspect_document("manual.docx", docx_bytes("1. 전입신고\n신분증과 신청서를 확인합니다."))
    assert result.file_type == "docx"
    assert "전입신고" in result.text


def test_inspection_blocks_phone_number_without_exposing_value():
    try:
        inspect_document("manual.docx", docx_bytes("담당자 연락처 010-2345-6789"))
        raise AssertionError("개인정보가 포함된 문서가 허용됨")
    except DocumentRejected as exc:
        assert exc.code == "PERSONAL_INFORMATION_DETECTED"
        assert exc.findings[0].kind == "휴대전화번호"
        assert "010-2345-6789" not in exc.message


def test_inspection_blocks_phone_number_split_across_docx_runs():
    try:
        inspect_document("manual.docx", docx_runs_bytes(["연락처 ", "010-", "2345-", "6789"]))
        raise AssertionError("run으로 분리된 개인정보가 허용됨")
    except DocumentRejected as exc:
        assert exc.code == "PERSONAL_INFORMATION_DETECTED"


def test_inspection_blocks_unlabelled_passport_and_long_number():
    for text in ("M12345678", "123456789012"):
        try:
            inspect_document("manual.docx", docx_bytes(f"신청서 식별정보 {text}"))
            raise AssertionError("식별정보가 포함된 문서가 허용됨")
        except DocumentRejected as exc:
            assert exc.code == "PERSONAL_INFORMATION_DETECTED"


def test_inspection_accepts_text_only_hwpx():
    result = inspect_document("manual.hwpx", hwpx_bytes("1. 전입신고 신청서 확인"))
    assert result.file_type == "hwpx"
    assert "전입신고" in result.text


def test_inspection_accepts_standard_hwpx_preview_files():
    result = inspect_document("권한신청서.hwpx", hwpx_bytes("권한 신청 서식", standard_preview=True))
    assert result.file_type == "hwpx"


def test_inspection_blocks_extension_spoofing():
    try:
        inspect_document("manual.pdf", docx_bytes("정상 본문"))
        raise AssertionError("확장자 위장 파일이 허용됨")
    except DocumentRejected as exc:
        assert exc.code == "SIGNATURE_MISMATCH"


def test_inspection_blocks_embedded_docx_object():
    content = docx_bytes("정상 본문", {"word/embeddings/object1.bin": b"hidden"})
    try:
        inspect_document("manual.docx", content)
        raise AssertionError("내장 객체 문서가 허용됨")
    except DocumentRejected as exc:
        assert exc.code == "EMBEDDED_OBJECT"


def test_inspection_blocks_docx_images():
    content = docx_bytes("정상 본문", {"word/media/image1.png": b"not-a-real-image"})
    try:
        inspect_document("manual.docx", content)
        raise AssertionError("검사할 수 없는 이미지가 허용됨")
    except DocumentRejected as exc:
        assert exc.code == "UNINSPECTABLE_IMAGE"


def test_upload_search_and_version_history(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    first = docx_bytes("Ⅰ. 주민등록시스템\n1. 전입신고\n신분증과 신청서를 확인합니다.")
    second = docx_bytes("Ⅰ. 주민등록시스템\n1. 전입신고\n계약서 확인 절차를 추가합니다.")

    with TestClient(app) as client:
        response = upload(client, first)
        assert response.status_code == 200
        document_id = response.json()["document_id"]
        assert response.json()["version"] == 1

        search = client.get("/api/documents", params={"q": "신분증"}).json()
        assert search["total"] == 1
        slug = search["items"][0]["slug"]
        detail = client.get(f"/api/documents/{slug}").json()
        assert detail["title"] == "전입신고 업무"
        assert detail["downloadable"] is True
        assert len(detail["outline"]) >= 2

        response = upload(
            client,
            second,
            existing_document_id=str(document_id),
            change_note="계약서 확인 절차 추가",
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2
        versions = client.get(f"/api/documents/{slug}/versions").json()
        assert [item["version"] for item in versions] == [2, 1]
        audits = client.get(
            "/api/admin/audits",
            headers={"X-Admin-Key": "test-admin-key"},
        ).json()["items"]
        assert audits[0]["actor"] == "테스트 담당자"


def test_blocked_upload_is_written_to_safe_audit_log(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = docx_bytes("문의 010-9876-5432")
    with TestClient(app) as client:
        response = upload(client, content)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PERSONAL_INFORMATION_DETECTED"
        audits = client.get(
            "/api/admin/audits",
            headers={"X-Admin-Key": "test-admin-key"},
        ).json()["items"]
        assert audits[0]["result"] == "blocked"
        assert "010-9876-5432" not in str(audits[0])


def test_personal_information_in_actor_is_not_written_to_audit_log(tmp_path, monkeypatch):
    configure_paths(tmp_path, monkeypatch)
    content = docx_bytes("개인정보가 없는 정상 업무 절차")
    with TestClient(app) as client:
        response = upload(client, content, admin_name="010-1234-5678")
        assert response.status_code == 422
        audits = client.get(
            "/api/admin/audits",
            headers={"X-Admin-Key": "test-admin-key"},
        ).json()["items"]
        assert audits[0]["actor"] == "차단된 입력"
        assert "010-1234-5678" not in str(audits[0])

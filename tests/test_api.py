from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

import main
from app.documents import (
    DownloadedAttachment,
    cache_downloaded_attachment,
    clear_downloaded_attachment_cache,
    extract_attachment,
)
from app.errors import IntegrationError
from app.fracttal import AttachmentMetadata, FracttalClient
from app.settings import load_settings


@pytest.fixture()
def configured_settings(monkeypatch):
    base = load_settings()
    configured = replace(
        base,
        api_secret_token="test-secret",
        fracttal_basic_key="key",
        fracttal_basic_secret="secret",
        document_gemini_api_key=None,
        ocr_with_gemini=False,
    )
    monkeypatch.setattr(main, "settings", configured)
    return configured


@pytest.fixture()
def client(configured_settings):
    return TestClient(main.app)


def auth_headers():
    return {"Authorization": "Bearer test-secret"}


def test_health_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_private_endpoint_requires_token(client):
    response = client.get("/check_health")
    assert response.status_code == 401


def test_bridge_rejects_external_url(client):
    response = client.get(
        "/api/bridge",
        headers=auth_headers(),
        params={"url": "https://example.com/api/items", "token": "abc"},
    )
    assert response.status_code == 400


def test_metadata_endpoint_contract(client, monkeypatch):
    items = [
        AttachmentMetadata(1, 11, "autorizacao.pdf", "https://fracttal-fs.s3.amazonaws.com/a"),
        AttachmentMetadata(2, 11, "email.jpg", "https://fracttal-fs.s3.amazonaws.com/b"),
    ]

    def fake_list(self, code, **kwargs):
        assert code == "11"
        return items, 2

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["total"] == 2
    assert "signed_path_image" not in body["data"][0]
    assert body["data"][0]["download_disponivel"] is True


def test_processed_endpoint_returns_erp_key(client, monkeypatch):
    item = AttachmentMetadata(1, 11, "autorizacao.txt", "https://fracttal-fs.s3.amazonaws.com/a")

    def fake_list(self, code, **kwargs):
        return [item], 1

    class FakeExtracted:
        id = 1
        mime_type = "text/plain"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 1,
                "id_request": 11,
                "description": "autorizacao.txt",
                "texto_extraido": "De acordo",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "CONCLUIDO"
    assert body["anexos_autorizacao"][0]["texto_extraido"] == "De acordo"


def test_no_attachments_is_success_not_integration_error(client, monkeypatch):
    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([], 0),
    )
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processing_status"] == "SEM_ANEXOS"
    assert body["anexos_autorizacao"] == []


def test_integration_error_is_structured(client, monkeypatch):
    def fail(self, code, **kwargs):
        raise IntegrationError(
            "fracttal_authentication_error",
            "Credenciais recusadas.",
            upstream_status=401,
        )

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fail)
    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos",
        headers=auth_headers(),
    )
    assert response.status_code == 502
    body = response.json()
    assert body["success"] is False
    assert body["error_type"] == "fracttal_authentication_error"
    assert body["data"] == []


def test_plain_text_extraction(configured_settings):
    metadata = AttachmentMetadata(1, 11, "autorizacao.txt", "https://example.invalid")
    content = "Estou ciente e de acordo para cópia de chaves.".encode("utf-8")
    downloaded = DownloadedAttachment(
        metadata=metadata,
        content=content,
        mime_type="text/plain",
        sha256_hex="abc",
    )
    extracted = extract_attachment(downloaded, configured_settings)
    assert extracted.status_extracao == "EXTRAIDO_LOCALMENTE"
    assert "de acordo" in extracted.texto_extraido
    assert extracted.metodo_extracao == "TEXTO_LOCAL"
    assert extracted.evidencias
    assert "decisão final permanece no ERP" in extracted.interpretacao


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_fracttal_pagination(configured_settings):
    first = {
        "success": True,
        "data": [
            {
                "id": 1,
                "id_request": 11,
                "description": "a.jpg",
                "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/a",
            }
        ],
        "total": 2,
    }
    second = {
        "success": True,
        "data": [
            {
                "id": 2,
                "id_request": 11,
                "description": "b.pdf",
                "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/b",
            }
        ],
        "total": 2,
    }
    session = FakeSession([FakeResponse(first), FakeResponse(second)])
    client = FracttalClient(configured_settings, session=session)
    attachments, total = client.list_request_attachments("11", limit=1)
    assert total == 2
    assert [item.id for item in attachments] == [1, 2]
    assert session.calls[1][1]["params"]["start"] == 1


def test_invalid_request_code(configured_settings):
    client = FracttalClient(configured_settings, session=FakeSession([]))
    with pytest.raises(IntegrationError) as exc:
        client.list_request_attachments("../../etc/passwd")
    assert exc.value.error_type == "invalid_request_code"



def test_processed_image_contains_signed_preview_path(client, monkeypatch):
    item = AttachmentMetadata(
        8,
        11,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )

    class FakeExtracted:
        id = 8
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 8,
                "id_request": 11,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])

    response = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    preview = response.json()["anexos_autorizacao"][0]["imagem_analisada"]
    parsed = urlparse(preview)
    assert parsed.path == "/api/fracttal/solicitacoes/11/anexos/8/visualizacao"
    query = parse_qs(parsed.query)
    assert int(query["expires"][0]) > 0
    assert len(query["signature"][0]) == 64


def test_signed_preview_endpoint_returns_image(client, monkeypatch):
    item = AttachmentMetadata(
        8,
        11,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )
    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    downloaded = DownloadedAttachment(
        metadata=item,
        content=b"fake-image",
        mime_type="image/jpeg",
        sha256_hex="abc",
    )
    class FakeExtracted:
        id = 8
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 8,
                "id_request": 11,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])
    monkeypatch.setattr(main, "download_attachment", lambda metadata, settings: downloaded)
    monkeypatch.setattr(main, "render_attachment_preview", lambda value: (b"preview", "image/jpeg"))

    processed = client.get(
        "/api/fracttal/solicitacoes/11/anexos/processados",
        headers=auth_headers(),
    )
    preview = processed.json()["anexos_autorizacao"][0]["imagem_analisada"]
    response = client.get(preview)
    assert response.status_code == 200
    assert response.content == b"preview"
    assert response.headers["content-type"].startswith("image/jpeg")


def test_legacy_fracttal_credentials_are_available_without_render_env(monkeypatch):
    for name in (
        "FRACTTAL_BASIC_TOKEN",
        "FRACTTAL_BASIC_KEY",
        "FRACTTAL_BASIC_SECRET",
        "FRACTTAL_KEY",
        "FRACTTAL_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    loaded = load_settings()
    assert loaded.fracttal_basic_key
    assert loaded.fracttal_basic_secret


def test_processed_image_contains_separate_original_download_path(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "autorizacao.pdf",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.pdf",
    )

    class FakeExtracted:
        id = 18
        mime_type = "application/pdf"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "autorizacao.pdf",
                "nome": "autorizacao.pdf",
                "mime_type": "application/pdf",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )

    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    data = response.json()["anexos_autorizacao"][0]

    preview = urlparse(data["imagem_analisada"])
    download = urlparse(data["arquivo_original_url"])
    assert preview.path.endswith("/visualizacao")
    assert download.path.endswith("/download")
    assert data["arquivo_url"] == data["imagem_analisada"]
    assert parse_qs(preview.query)["signature"][0] != parse_qs(download.query)["signature"][0]


def test_signed_download_endpoint_returns_original_file_as_attachment(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "Autorização chaveiro.pdf",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.pdf",
    )

    class FakeExtracted:
        id = 18
        mime_type = "application/pdf"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "Autorização chaveiro.pdf",
                "nome": "Autorização chaveiro.pdf",
                "mime_type": "application/pdf",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )
    monkeypatch.setattr(
        main,
        "download_attachment",
        lambda metadata, settings: DownloadedAttachment(
            metadata=metadata,
            content=b"%PDF-original-content",
            mime_type="application/pdf",
            sha256_hex="abc",
        ),
    )

    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    download_url = processed.json()["anexos_autorizacao"][0]["arquivo_original_url"]
    response = client.get(download_url)

    assert response.status_code == 200
    assert response.content == b"%PDF-original-content"
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_preview_signature_cannot_be_reused_for_download(client, monkeypatch):
    item = AttachmentMetadata(
        18,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )

    class FakeExtracted:
        id = 18
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 18,
                "id_request": 51329,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )

    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    preview = urlparse(processed.json()["anexos_autorizacao"][0]["imagem_analisada"])
    forged_download = preview._replace(path=preview.path.replace("/visualizacao", "/download")).geturl()

    response = client.get(forged_download)
    assert response.status_code == 403
    assert response.json()["error_type"] == "attachment_access_invalid_signature"

def test_fracttal_header_overrides_server_credentials(client, monkeypatch):
    received = []

    def fake_list(self, code, **kwargs):
        received.append(self._authorization_value())
        return [], 0

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    token = "dXNlci1lcnA6c2VjcmV0LWVycA=="
    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers={
            **auth_headers(),
            "X-Fracttal-Authorization": f"Basic {token}",
        },
    )
    assert response.status_code == 200
    assert received == [f"Basic {token}"]


def test_invalid_forwarded_fracttal_header_is_rejected(client):
    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers={
            **auth_headers(),
            "X-Fracttal-Authorization": "Basic credencial-invalida",
        },
    )
    assert response.status_code == 422
    assert response.json()["error_type"] == "invalid_fracttal_authorization"


def test_preview_reuses_forwarded_fracttal_header_without_render_credentials(
    client, configured_settings, monkeypatch
):
    item = AttachmentMetadata(
        81,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/autorizacao.jpg",
    )
    without_fracttal_env = replace(
        configured_settings,
        fracttal_basic_token=None,
        fracttal_basic_key=None,
        fracttal_basic_secret=None,
    )
    monkeypatch.setattr(main, "settings", without_fracttal_env)
    main._ATTACHMENT_AUTH_CACHE.clear()
    received = []

    def fake_list(self, code, **kwargs):
        received.append(self._authorization_value())
        return [item], 1

    class FakeExtracted:
        id = 81
        mime_type = "image/jpeg"
        status_extracao = "EXTRAIDO"

        def to_erp_dict(self):
            return {
                "id": 81,
                "id_request": 51329,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "Autorizado",
                "status_extracao": "EXTRAIDO",
            }

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    monkeypatch.setattr(
        main,
        "process_attachments",
        lambda attachments, settings: [FakeExtracted()],
    )
    monkeypatch.setattr(
        main,
        "download_attachment",
        lambda metadata, settings: DownloadedAttachment(
            metadata=metadata,
            content=b"original",
            mime_type="image/jpeg",
            sha256_hex="abc",
        ),
    )
    monkeypatch.setattr(
        main, "render_attachment_preview", lambda value: (b"preview", "image/jpeg")
    )

    token = "dXNlci1lcnA6c2VjcmV0LWVycA=="
    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers={
            **auth_headers(),
            "X-Fracttal-Authorization": token,
        },
    )
    assert processed.status_code == 200
    preview_url = processed.json()["anexos_autorizacao"][0]["imagem_analisada"]

    preview = client.get(preview_url)
    assert preview.status_code == 200
    assert preview.content == b"preview"
    assert received == [f"Basic {token}", f"Basic {token}"]

def test_attachment_query_uses_only_documented_code_path(configured_settings):
    payload = {
        "success": True,
        "data": [{
            "id": 9,
            "id_request": 51329,
            "description": "autorizacao.jpg",
            "signed_path_image": "https://fracttal-fs.s3.amazonaws.com/a",
        }],
        "total": 1,
    }
    session = FakeSession([FakeResponse(payload)])
    fracttal = FracttalClient(configured_settings, session=session)
    attachments, total = fracttal.list_request_attachments("51329")
    assert total == 1
    assert attachments[0].id == 9
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url.endswith("/work_requests_attachments/51329")
    assert kwargs["params"] == {"start": 0, "limit": 100}


def test_attachment_404_contains_exact_context(client, monkeypatch):
    def fail(self, code, **kwargs):
        raise IntegrationError(
            "fracttal_request_not_found",
            (
                "ERRO DE INTEGRAÇÃO — o Fracttal respondeu HTTP 404 para o "
                "code 51329. A solicitação não foi localizada ou o identificador "
                "enviado não corresponde ao campo code."
            ),
            status_code=404,
            upstream_status=404,
            stage="consultar_anexos_fracttal",
            request_code="51329",
            endpoint="work_requests_attachments/51329",
        )

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fail)
    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["stage"] == "consultar_anexos_fracttal"
    assert body["error_type"] == "fracttal_request_not_found"
    assert body["request_code"] == "51329"
    assert body["fracttal_status"] == 404
    assert body["endpoint"] == "work_requests_attachments/51329"
    assert "campo code" in body["message"]



class DownloadResponse:
    def __init__(self, content: bytes, *, status_code: int = 200, content_type: str = "image/jpeg"):
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
        }
        self.url = "https://fracttal-fs.s3.amazonaws.com/company/request/file"
        self._content = content

    def iter_content(self, chunk_size=65536):
        del chunk_size
        if self._content:
            yield self._content

    def close(self):
        return None


class DownloadSession:
    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        del args, kwargs
        return self.response

    def close(self):
        return None


def test_download_diagnostics_and_unexpected_html(configured_settings, monkeypatch):
    import app.documents as documents

    metadata = AttachmentMetadata(
        91,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/file",
    )
    response = DownloadResponse(
        b"<html><body>AccessDenied</body></html>",
        content_type="text/html",
    )
    monkeypatch.setattr(documents, "build_session", lambda settings: DownloadSession(response))

    result = documents.process_attachment(metadata, configured_settings)
    assert result.status_extracao == "URL_EXPIRADA"
    assert result.download_status == 200
    assert result.error_type == "attachment_signed_url_expired"
    assert result.error_stage == "baixar_anexo_fracttal"
    assert "página de erro" in " ".join(result.avisos)


def test_successful_download_reports_http_mime_size_and_method(configured_settings, monkeypatch):
    import app.documents as documents

    metadata = AttachmentMetadata(
        92,
        51329,
        "autorizacao.txt",
        "https://fracttal-fs.s3.amazonaws.com/file",
    )
    content = b"Responsavel autorizado e de acordo com a copia de chaves."
    response = DownloadResponse(content, content_type="text/plain; charset=utf-8")
    monkeypatch.setattr(documents, "build_session", lambda settings: DownloadSession(response))

    result = documents.process_attachment(metadata, configured_settings)
    assert result.status_extracao == "EXTRAIDO_LOCALMENTE"
    assert result.download_status == 200
    assert result.mime_type == "text/plain"
    assert result.tamanho_bytes == len(content)
    assert result.metodo_extracao == "TEXTO_LOCAL"
    assert result.evidencias


def test_processed_endpoint_reuses_recent_gemini_key_from_same_erp(client, monkeypatch):
    main._DOCUMENT_GEMINI_KEY_CACHE.clear()
    monkeypatch.setattr(main, "_legacy_gemini_execute", lambda *args: (True, "{}"))
    gemini_key = "AQ.test-document-key"
    warm = client.get(
        "/api/executar",
        headers=auth_headers(),
        params={
            "api_key": gemini_key,
            "modelo": "gemini-2.5-flash-lite",
            "CONTEXTO_CLASSIFICACAO": "teste",
            "texto": "teste",
        },
    )
    assert warm.status_code == 200

    item = AttachmentMetadata(
        93,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/file",
    )
    monkeypatch.setattr(
        FracttalClient,
        "list_request_attachments",
        lambda self, code, **kwargs: ([item], 1),
    )
    captured = {}

    class FakeExtracted:
        id = 93
        mime_type = "image/jpeg"
        status_extracao = "OCR_REALIZADO"

        def to_erp_dict(self):
            return {
                "id": 93,
                "id_request": 51329,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "De acordo",
                "status_extracao": "OCR_REALIZADO",
            }

    def fake_process(attachments, settings):
        captured["key"] = settings.document_gemini_api_key
        captured["enabled"] = settings.ocr_with_gemini
        return [FakeExtracted()]

    monkeypatch.setattr(main, "process_attachments", fake_process)
    response = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    assert response.status_code == 200
    assert captured == {"key": gemini_key, "enabled": True}
    body = response.json()
    assert body["ocr_disponivel"] is True
    assert body["fonte_chave_ocr"] == "recent_gemini_request"


def test_preview_uses_file_cached_during_processing_without_second_fracttal_query(
    client, configured_settings, monkeypatch
):
    item = AttachmentMetadata(
        94,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/file",
    )
    clear_downloaded_attachment_cache()
    cache_downloaded_attachment(
        DownloadedAttachment(
            metadata=item,
            content=b"cached-original",
            mime_type="image/jpeg",
            sha256_hex="abc",
        ),
        configured_settings,
        request_code="51329",
    )
    calls = {"fracttal": 0}

    def fake_list(self, code, **kwargs):
        calls["fracttal"] += 1
        if calls["fracttal"] > 1:
            raise AssertionError("A prévia não deve consultar novamente o Fracttal")
        return [item], 1

    class FakeExtracted:
        id = 94
        mime_type = "image/jpeg"
        status_extracao = "OCR_REALIZADO"

        def to_erp_dict(self):
            return {
                "id": 94,
                "id_request": 51329,
                "description": "autorizacao.jpg",
                "mime_type": "image/jpeg",
                "texto_extraido": "De acordo",
                "status_extracao": "OCR_REALIZADO",
            }

    monkeypatch.setattr(FracttalClient, "list_request_attachments", fake_list)
    monkeypatch.setattr(main, "process_attachments", lambda attachments, settings: [FakeExtracted()])
    monkeypatch.setattr(main, "render_attachment_preview", lambda value: (value.content, "image/jpeg"))

    processed = client.get(
        "/api/fracttal/solicitacoes/51329/anexos/processados",
        headers=auth_headers(),
    )
    preview_url = processed.json()["anexos_autorizacao"][0]["imagem_analisada"]
    preview = client.get(preview_url)
    assert preview.status_code == 200
    assert preview.content == b"cached-original"
    assert calls["fracttal"] == 1



def test_image_ocr_success_has_explicit_status_and_interpretation(configured_settings, monkeypatch):
    import app.documents as documents
    from dataclasses import replace

    metadata = AttachmentMetadata(
        95,
        51329,
        "autorizacao.jpg",
        "https://fracttal-fs.s3.amazonaws.com/file",
    )
    downloaded = DownloadedAttachment(
        metadata=metadata,
        content=b"\xff\xd8\xfffake",
        mime_type="image/jpeg",
        sha256_hex="abc",
        download_status=200,
        response_content_type="image/jpeg",
        final_host="fracttal-fs.s3.amazonaws.com",
    )
    settings = replace(
        configured_settings,
        document_gemini_api_key="AQ.test",
        ocr_with_gemini=True,
    )
    monkeypatch.setattr(
        documents,
        "_transcribe_with_gemini",
        lambda content, mime_type, settings: (
            "Responsável de acordo e autorização aprovada para cópia de chaves.",
            "",
        ),
    )
    result = documents.extract_attachment(downloaded, settings)
    assert result.status_extracao == "OCR_REALIZADO"
    assert result.metodo_extracao == "OCR_GEMINI_IMAGEM"
    assert result.download_status == 200
    assert result.evidencias
    assert "decisão final permanece no ERP" in result.interpretacao


def _minimal_report_bundle() -> bytes:
    import json
    import zipfile

    buffer = BytesIO()
    report = {
        "metadata": {
            "title": "Parecer técnico",
            "period": "01/05/2026 a 30/07/2026",
            "generatedAt": "30/07/2026 15:00",
            "site": "Todas",
            "source": "Fracttal One",
            "version": "Rev. 01",
        },
        "executiveMetrics": [],
        "sensors": [],
        "completedOrders": [],
        "pendingTasks": [],
        "generation": {"status": "ready", "progress": 100, "message": "Pronto", "updatedAt": "2026-07-30T15:00:00-03:00"},
    }
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", "<!doctype html><script src='./assets/app.js'></script>")
        archive.writestr("assets/app.js", "fetch('./report-data.json')")
        archive.writestr("assets/app.css", "body{}")
        archive.writestr("report-data.json", json.dumps(report))
    return buffer.getvalue()


def test_report_share_is_public_for_48_hours_without_recipient_credentials(client, configured_settings, monkeypatch, tmp_path):
    from app.report_sharing import build_report_store

    share_settings = replace(
        configured_settings,
        report_upload_token="report-upload-secret",
        report_share_signing_secret="server-side-signing-secret-with-more-than-32-chars",
        report_share_public_base_url="https://conversor-siff.onrender.com",
        report_share_storage_backend="local",
        report_share_local_dir=str(tmp_path / "reports"),
    )
    monkeypatch.setattr(main, "settings", share_settings)
    build_report_store.cache_clear()

    response = client.post(
        "/api/reports/share",
        headers={
            "Authorization": "Bearer report-upload-secret",
            "Content-Type": "application/zip",
            "X-Report-Name": "CENTRAL ANAYTICS",
            "X-Report-Period": "01/05/2026 a 30/07/2026",
        },
        content=_minimal_report_bundle(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["valid_hours"] == 48
    assert body["requires_credentials"] is False
    assert body["share_url"].startswith("https://conversor-siff.onrender.com/relatorios/")
    assert "report-upload-secret" not in response.text
    assert "server-side-signing-secret" not in response.text

    token = body["share_token"]
    assert body["share_url"].endswith("/")
    redirect = client.get(f"/relatorios/{token}", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"/relatorios/{token}/"

    public_html = client.get(body["share_url"])
    assert public_html.status_code == 200
    assert "Authorization" not in public_html.request.headers
    assert public_html.headers["referrer-policy"] == "no-referrer"
    assert public_html.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in public_html.headers["content-security-policy"]
    assert "report-upload-secret" not in public_html.text
    assert "server-side-signing-secret" not in public_html.text
    public_js = client.get(f"/relatorios/{token}/assets/app.js")
    assert public_js.status_code == 200
    assert "fetch('./report-data.json')" in public_js.text

    public_data = client.get(f"/relatorios/{token}/report-data.json")
    assert public_data.status_code == 200
    sharing = public_data.json()["sharing"]
    assert sharing["validHours"] == 48
    assert sharing["requiresCredentials"] is False
    assert sharing["publicAccess"] is True
    assert sharing["expiresAt"] == body["expires_at"]


def test_report_share_creation_and_revocation_require_upload_token(client, configured_settings, monkeypatch, tmp_path):
    from app.report_sharing import build_report_store

    share_settings = replace(
        configured_settings,
        report_upload_token="dedicated-token",
        report_share_signing_secret="another-server-side-signing-secret-long-enough",
        report_share_storage_backend="local",
        report_share_local_dir=str(tmp_path / "reports"),
    )
    monkeypatch.setattr(main, "settings", share_settings)
    build_report_store.cache_clear()

    denied = client.post("/api/reports/share", content=_minimal_report_bundle())
    assert denied.status_code == 401

    # O token dedicado continua aceito. A credencial principal já usada pelo
    # ERP também permanece válida, evitando uma segunda configuração local.
    created = client.post(
        "/api/reports/share",
        headers={"Authorization": "Bearer test-secret", "Content-Type": "application/zip"},
        content=_minimal_report_bundle(),
    )
    assert created.status_code == 201
    token = created.json()["share_token"]
    assert client.delete(f"/api/reports/share/{token}").status_code == 401
    assert client.delete(
        f"/api/reports/share/{token}",
        headers={"Authorization": "Bearer dedicated-token"},
    ).status_code == 200
    assert client.get(f"/relatorios/{token}").status_code == 404


def test_report_token_expires_exactly_after_48_hours(configured_settings, tmp_path):
    from app.report_sharing import (
        REPORT_SHARE_TTL_SECONDS,
        build_report_store,
        publish_report,
        read_report_resource,
    )

    share_settings = replace(
        configured_settings,
        report_share_signing_secret="expiry-signing-secret-with-more-than-32-characters",
        report_share_storage_backend="local",
        report_share_local_dir=str(tmp_path / "reports"),
    )
    build_report_store.cache_clear()
    created_at = 1_800_000_000
    published = publish_report(
        _minimal_report_bundle(),
        share_settings,
        public_base_url="https://api.example",
        report_name="CENTRAL ANAYTICS",
        report_period="três meses",
        now=created_at,
    )
    content, media_type, _payload = read_report_resource(
        published.token,
        "index.html",
        share_settings,
        public_base_url="https://api.example",
        now=created_at + REPORT_SHARE_TTL_SECONDS - 1,
    )
    assert media_type == "text/html"
    assert b"doctype html" in content
    with pytest.raises(Exception) as exc:
        read_report_resource(
            published.token,
            "index.html",
            share_settings,
            public_base_url="https://api.example",
            now=created_at + REPORT_SHARE_TTL_SECONDS,
        )
    assert getattr(exc.value, "status_code", None) == 410

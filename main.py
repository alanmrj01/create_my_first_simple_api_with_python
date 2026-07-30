from __future__ import annotations

"""API central do ERP de manutenção.

Compatibilidade preservada:
- GET /api/bridge
- GET /api/executar
- GET /check_health

Novos endpoints especializados:
- GET /api/fracttal/solicitacoes/{code}/anexos
- GET /api/fracttal/solicitacoes/{code}/anexos/processados

Os endpoints especializados usam credenciais do Fracttal armazenadas no Render,
tratam paginação e erros de forma explícita e nunca confundem falha de
integração com ausência real de anexos.
"""

import os
from dataclasses import replace
from hashlib import sha256
from pathlib import PurePath
from threading import RLock
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import requests

from app.documents import (
    alias_cached_attachment,
    download_attachment,
    get_cached_downloaded_attachment,
    process_attachments,
    render_attachment_preview,
)
from app.errors import IntegrationError
from app.fracttal import FracttalClient, normalize_fracttal_authorization
from app.http import build_session
from app.preview import (
    build_download_path,
    build_preview_path,
    verify_download,
    verify_preview,
)
from app.security import (
    require_api_token,
    require_report_upload_token,
    validate_fracttal_bridge_url,
)
from app.settings import Settings, load_settings
from app.report_sharing import (
    REPORT_SHARE_TTL_HOURS,
    ReportBundleInvalid,
    ReportStoreUnavailable,
    publish_report,
    read_report_resource,
    revoke_report,
)

settings: Settings = load_settings()
security = HTTPBearer(auto_error=False)


# O ERP já envia uma chave Gemini ao endpoint legado /api/executar. Como o
# proprietário deste serviço compartilhado não consegue configurar novas
# variáveis no Render, a API reutiliza essa chave apenas em memória, vinculada
# ao mesmo token Bearer do ERP e por prazo curto, exclusivamente para OCR de
# anexos. A chave nunca é persistida, exibida em resposta ou escrita em log.
_DOCUMENT_GEMINI_KEY_CACHE: dict[str, tuple[int, str]] = {}
_DOCUMENT_GEMINI_KEY_CACHE_LOCK = RLock()
_DOCUMENT_GEMINI_KEY_TTL_SECONDS = 6 * 3600
_DOCUMENT_GEMINI_KEY_CACHE_MAX_ITEMS = 50


def _api_client_fingerprint(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    token = str(getattr(credentials, "credentials", "") or "").strip()
    if not token:
        return None
    return sha256(token.encode("utf-8")).hexdigest()


def _remember_document_gemini_key(
    credentials: HTTPAuthorizationCredentials | None, api_key: str
) -> None:
    fingerprint = _api_client_fingerprint(credentials)
    key = str(api_key or "").strip()
    if not fingerprint or not key:
        return
    now = int(time.time())
    expires = now + _DOCUMENT_GEMINI_KEY_TTL_SECONDS
    with _DOCUMENT_GEMINI_KEY_CACHE_LOCK:
        expired = [
            item_key
            for item_key, (item_expires, _item_value)
            in _DOCUMENT_GEMINI_KEY_CACHE.items()
            if item_expires < now
        ]
        for item_key in expired:
            _DOCUMENT_GEMINI_KEY_CACHE.pop(item_key, None)
        _DOCUMENT_GEMINI_KEY_CACHE[fingerprint] = (expires, key)
        if len(_DOCUMENT_GEMINI_KEY_CACHE) > _DOCUMENT_GEMINI_KEY_CACHE_MAX_ITEMS:
            oldest = sorted(
                _DOCUMENT_GEMINI_KEY_CACHE.items(), key=lambda item: item[1][0]
            )
            excess = (
                len(_DOCUMENT_GEMINI_KEY_CACHE)
                - _DOCUMENT_GEMINI_KEY_CACHE_MAX_ITEMS
            )
            for item_key, _value in oldest[:excess]:
                _DOCUMENT_GEMINI_KEY_CACHE.pop(item_key, None)


def _recent_document_gemini_key(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    fingerprint = _api_client_fingerprint(credentials)
    if not fingerprint:
        return None
    now = int(time.time())
    with _DOCUMENT_GEMINI_KEY_CACHE_LOCK:
        cached = _DOCUMENT_GEMINI_KEY_CACHE.get(fingerprint)
        if cached is None:
            return None
        expires, key = cached
        if expires < now:
            _DOCUMENT_GEMINI_KEY_CACHE.pop(fingerprint, None)
            return None
        return key


def _document_processing_settings(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[Settings, str]:
    if settings.document_gemini_api_key:
        return settings, "environment"
    recent_key = _recent_document_gemini_key(credentials)
    if recent_key:
        return (
            replace(
                settings,
                document_gemini_api_key=recent_key,
                ocr_with_gemini=True,
            ),
            "recent_gemini_request",
        )
    return settings, "unavailable"


# O componente de imagem do ERP acessa as URLs assinadas sem headers próprios.
# Quando o endpoint processado recebe a credencial do Fracttal do ERP, ela é
# mantida somente em memória durante o mesmo prazo da URL temporária, permitindo
# que a prévia e o download reutilizem a autenticação sem qualquer ajuste no
# Render e sem expor a credencial na URL.
_ATTACHMENT_AUTH_CACHE: dict[tuple[str, int], tuple[int, str]] = {}
_ATTACHMENT_AUTH_CACHE_LOCK = RLock()
_ATTACHMENT_AUTH_CACHE_MAX_ITEMS = 1000


def _remember_attachment_authorization(
    code: str,
    attachment_ids: list[int],
    authorization: str | None,
) -> None:
    normalized = normalize_fracttal_authorization(authorization)
    if not normalized or not attachment_ids:
        return
    now = int(time.time())
    expires = now + settings.attachment_preview_ttl_seconds
    with _ATTACHMENT_AUTH_CACHE_LOCK:
        expired = [
            key for key, (item_expires, _value) in _ATTACHMENT_AUTH_CACHE.items()
            if item_expires < now
        ]
        for key in expired:
            _ATTACHMENT_AUTH_CACHE.pop(key, None)
        for attachment_id in attachment_ids:
            _ATTACHMENT_AUTH_CACHE[(str(code), int(attachment_id))] = (
                expires,
                normalized,
            )
        if len(_ATTACHMENT_AUTH_CACHE) > _ATTACHMENT_AUTH_CACHE_MAX_ITEMS:
            oldest = sorted(
                _ATTACHMENT_AUTH_CACHE.items(),
                key=lambda item: item[1][0],
            )
            excess = len(_ATTACHMENT_AUTH_CACHE) - _ATTACHMENT_AUTH_CACHE_MAX_ITEMS
            for key, _value in oldest[:excess]:
                _ATTACHMENT_AUTH_CACHE.pop(key, None)


def _attachment_authorization(code: str, attachment_id: int) -> str | None:
    now = int(time.time())
    key = (str(code), int(attachment_id))
    with _ATTACHMENT_AUTH_CACHE_LOCK:
        cached = _ATTACHMENT_AUTH_CACHE.get(key)
        if cached is None:
            return None
        expires, authorization = cached
        if expires < now:
            _ATTACHMENT_AUTH_CACHE.pop(key, None)
            return None
        return authorization


app = FastAPI(
    title="ERP Manutenção — API Central",
    version="2.1.0",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/relatorios/"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'none'"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    else:
        response.headers[
            "Content-Security-Policy"
        ] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(IntegrationError)
async def integration_error_handler(_request: Request, exc: IntegrationError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "stage": exc.stage,
            "error_type": exc.error_type,
            "message": exc.message,
            "request_code": exc.request_code,
            "fracttal_status": exc.upstream_status,
            "upstream_status": exc.upstream_status,
            "endpoint": exc.endpoint,
            "data": [],
        },
    )


def _authorize(credentials: HTTPAuthorizationCredentials | None) -> None:
    require_api_token(credentials, settings)


def _legacy_gemini_execute(
    api_key: str,
    modelo: str,
    contexto_classificacao: str,
    texto: str,
) -> tuple[bool, str]:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        chat = client.chats.create(
            model=modelo,
            config=types.GenerateContentConfig(
                system_instruction=contexto_classificacao,
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        resposta = chat.send_message(texto)
        return True, str(resposta.text or "")
    except Exception as exc:
        return False, str(exc)


def _legacy_bridge(url: str, token: str) -> list[Any]:
    """Mantém o contrato legado do ERP sem esconder URLs externas livres.

    A resposta continua sendo uma lista no campo ``resultado``. Os novos
    endpoints especializados são os que oferecem erro estruturado.
    """
    validate_fracttal_bridge_url(url, settings)
    headers = {"Authorization": f"Basic {token}"}
    session = build_session(settings)
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=(settings.connect_timeout, settings.read_timeout),
        )
        if response.status_code != 200:
            return []
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError):
        return []
    finally:
        session.close()


@app.get("/health")
def public_health() -> dict[str, str]:
    """Health check público, sem segredos ou acesso a integrações externas."""
    return {"status": "ok", "service": "erp-central-api", "version": "2.1.0"}


@app.get("/check_health")
def check_health_api(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    _authorize(credentials)
    return {"status": "ok", "description": "Api check health", "version": "2.1.0"}


@app.get("/api/executar")
def executar_funcao(
    api_key: str,
    modelo: str,
    CONTEXTO_CLASSIFICACAO: str,
    texto: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Endpoint legado do Gemini, preservado sem mudança de contrato."""
    _authorize(credentials)
    _remember_document_gemini_key(credentials, api_key)
    resultado = _legacy_gemini_execute(
        api_key,
        modelo,
        CONTEXTO_CLASSIFICACAO,
        texto,
    )
    return {"resultado": resultado}


@app.get("/api/bridge")
def executar_bridge(
    url: str,
    token: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Bridge legado restrito à API oficial do Fracttal."""
    _authorize(credentials)
    return {"resultado": _legacy_bridge(url, token)}


@app.get("/api/fracttal/solicitacoes/{code}/anexos")
@app.get(
    "/api/fracttal/work-requests/{code}/attachments",
    include_in_schema=False,
)
def consultar_anexos_solicitacao(
    code: str,
    start: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    paginate_all: bool = Query(True),
    include_signed_url: bool = Query(False),
    fracttal_authorization: str | None = Header(
        None, alias="X-Fracttal-Authorization"
    ),
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Consulta metadados dos anexos de uma solicitação.

    Por segurança, URLs assinadas não são devolvidas por padrão. O endpoint de
    processamento baixa os arquivos internamente e retorna apenas texto,
    metadados e status de extração necessários ao ERP.
    """
    _authorize(credentials)
    client = FracttalClient(
        settings, authorization_override=fracttal_authorization
    )
    try:
        attachments, source_total = client.list_request_attachments(
            code,
            start=start,
            limit=limit,
            paginate_all=paginate_all,
        )
    finally:
        client.close()
    return {
        "success": True,
        "message": "Anexos consultados com sucesso.",
        "code": code,
        "total": len(attachments),
        "source_total": source_total,
        "data": [item.public_dict(include_signed_url) for item in attachments],
    }


@app.get("/api/fracttal/solicitacoes/{code}/anexos/processados")
@app.get(
    "/api/fracttal/work-requests/{code}/attachments/processed",
    include_in_schema=False,
)
def processar_anexos_solicitacao(
    code: str,
    fracttal_authorization: str | None = Header(
        None, alias="X-Fracttal-Authorization"
    ),
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Baixa e extrai os anexos para a validação documental no ERP.

    O retorno usa a chave ``anexos_autorizacao`` esperada pelo ERP_22. Lista
    vazia significa que a consulta foi executada e nenhum anexo existe. Falhas
    de autenticação, rede ou formato retornam erro HTTP estruturado e nunca uma
    lista vazia enganosa.
    """
    _authorize(credentials)
    client = FracttalClient(
        settings, authorization_override=fracttal_authorization
    )
    try:
        attachments, source_total = client.list_request_attachments(
            code,
            start=0,
            limit=100,
            paginate_all=True,
        )
    finally:
        client.close()
    _remember_attachment_authorization(
        code,
        [item.id for item in attachments],
        fracttal_authorization,
    )
    document_settings, ocr_key_source = _document_processing_settings(credentials)
    extracted = process_attachments(attachments, document_settings)
    for item in extracted:
        alias_cached_attachment(code, item.id, document_settings)
    payload = [item.to_erp_dict() for item in extracted]
    metadata_by_id = {item.id: item for item in attachments}
    for item, data in zip(extracted, payload):
        metadata = metadata_by_id.get(item.id)
        if metadata is None:
            continue
        if item.mime_type.startswith("image/") or item.mime_type == "application/pdf":
            preview_path = build_preview_path(code, item.id, settings)
            download_path = build_download_path(code, item.id, settings)
            # Mantém os campos já reconhecidos pelo ERP para a visualização.
            data["imagem_analisada"] = preview_path
            data["arquivo_url"] = preview_path
            # Campo novo e separado para o futuro botão de download do original.
            # O ERP atual não interpreta este campo como uma segunda imagem.
            data["arquivo_original_url"] = download_path
    success_statuses = {"EXTRAIDO", "EXTRAIDO_LOCALMENTE", "OCR_REALIZADO"}
    error_statuses = {"ERRO_INTEGRACAO", "ERRO_DOWNLOAD", "URL_EXPIRADA"}
    extracted_count = sum(
        1 for item in extracted if item.status_extracao in success_statuses
    )
    error_count = sum(
        1 for item in extracted if item.status_extracao in error_statuses
    )
    manual_count = len(extracted) - extracted_count - error_count

    if not attachments:
        processing_status = "SEM_ANEXOS"
    elif error_count == len(extracted):
        processing_status = "ERRO_INTEGRACAO"
    elif error_count or manual_count:
        processing_status = "PARCIAL"
    else:
        processing_status = "CONCLUIDO"

    return {
        "success": True,
        "message": "Anexos processados para validação documental.",
        "code": code,
        "total": len(attachments),
        "source_total": source_total,
        "processing_status": processing_status,
        "anexos_extraidos": extracted_count,
        "anexos_validacao_manual": manual_count,
        "anexos_com_erro": error_count,
        "ocr_disponivel": bool(
            document_settings.ocr_with_gemini
            and document_settings.document_gemini_api_key
        ),
        "fonte_chave_ocr": ocr_key_source,
        "anexos_autorizacao": payload,
    }




def _safe_download_filename(description: str, attachment_id: int) -> str:
    filename = PurePath(str(description or "")).name.replace("\x00", "").strip()
    return (filename or f"anexo-{attachment_id}")[:255]


def _locate_and_download_attachment(code: str, attachment_id: int):
    cached = get_cached_downloaded_attachment(code, attachment_id)
    if cached is not None:
        return cached.metadata, cached
    client = FracttalClient(
        settings,
        authorization_override=_attachment_authorization(code, attachment_id),
    )
    try:
        attachments, _source_total = client.list_request_attachments(
            code, start=0, limit=100, paginate_all=True
        )
    finally:
        client.close()
    metadata = next((item for item in attachments if item.id == attachment_id), None)
    if metadata is None:
        raise IntegrationError(
            "attachment_not_found",
            "O anexo solicitado não foi localizado para esta solicitação.",
            status_code=404,
        )
    return metadata, download_attachment(metadata, settings)


@app.get(
    "/api/fracttal/solicitacoes/{code}/anexos/{attachment_id}/visualizacao",
    include_in_schema=False,
)
def visualizar_anexo_solicitacao(
    code: str,
    attachment_id: int,
    expires: int = Query(..., ge=1),
    signature: str = Query(..., min_length=32, max_length=128),
):
    """Entrega a foto analisada ou a primeira página do PDF ao frontend.

    O link é temporário e assinado, pois o componente de imagem do frontend não
    envia o header de autenticação. Nenhum endpoint legado ou regra de negócio é
    alterado por esta rota.
    """
    verify_preview(code, attachment_id, expires, signature, settings)
    metadata, downloaded = _locate_and_download_attachment(code, attachment_id)
    content, media_type = render_attachment_preview(downloaded)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="anexo-{attachment_id}"',
            "X-Content-Type-Options": "nosniff",
        },
    )

@app.get(
    "/api/fracttal/solicitacoes/{code}/anexos/{attachment_id}/download",
    include_in_schema=False,
)
def baixar_anexo_original(
    code: str,
    attachment_id: int,
    expires: int = Query(..., ge=1),
    signature: str = Query(..., min_length=32, max_length=128),
):
    """Baixa o arquivo original somente quando o usuário aciona o botão próprio.

    Esta rota nunca é usada pela prévia e responde com ``attachment`` para que
    imagens e PDFs sejam baixados apenas sob ação explícita do usuário.
    """
    verify_download(code, attachment_id, expires, signature, settings)
    metadata, downloaded = _locate_and_download_attachment(code, attachment_id)
    filename = _safe_download_filename(metadata.description, attachment_id)
    encoded_filename = quote(filename, safe="")
    return Response(
        content=downloaded.content,
        media_type=downloaded.mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="anexo-{attachment_id}"; '
                f"filename*=UTF-8''{encoded_filename}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _report_public_base_url(request: Request) -> str:
    configured = settings.report_share_public_base_url
    if configured:
        return configured.rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    if forwarded_proto in {"http", "https"} and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _report_link_error_page(title: str, message: str, *, status_code: int) -> HTMLResponse:
    safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
    safe_message = message.replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        status_code=status_code,
        content=f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#07111f;color:#e8eef8;font-family:Arial,sans-serif}}main{{width:min(560px,calc(100% - 40px));padding:32px;border:1px solid #263852;border-radius:14px;background:#0d1a2b;box-shadow:0 24px 70px #0008}}span{{display:inline-block;padding:7px 10px;border-radius:999px;background:#f5b94218;color:#f5b942;font-size:12px;font-weight:800}}h1{{font-size:24px;margin:18px 0 10px}}p{{color:#aebbd0;line-height:1.6;margin:0}}small{{display:block;margin-top:20px;color:#718198}}</style></head>
<body><main><span>CENTRAL ANAYTICS</span><h1>{safe_title}</h1><p>{safe_message}</p><small>Os links compartilhados são temporários e válidos por exatamente 48 horas.</small></main></body></html>""",
    )


@app.post("/api/reports/share", status_code=201)
async def compartilhar_relatorio(
    request: Request,
    report_name: str | None = Header(None, alias="X-Report-Name"),
    report_period: str | None = Header(None, alias="X-Report-Period"),
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    """Publica um pacote estático por 48 horas; somente este endpoint exige token."""
    require_report_upload_token(credentials, settings)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.report_share_max_bundle_bytes:
                raise HTTPException(status_code=413, detail="Pacote de relatório acima do limite permitido")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length inválido") from None
    bundle = await request.body()
    try:
        published = publish_report(
            bundle,
            settings,
            public_base_url=_report_public_base_url(request),
            report_name=(report_name or "CENTRAL ANAYTICS")[:180],
            report_period=(report_period or "Período não informado")[:180],
        )
    except ReportBundleInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReportStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "success": True,
        "share_url": published.public_url,
        "share_token": published.token,
        "created_at": published.created_at.isoformat(),
        "expires_at": published.expires_at.isoformat(),
        "valid_hours": REPORT_SHARE_TTL_HOURS,
        "requires_credentials": False,
        "message": "Link temporário criado. O acesso é público para quem possuir o link e expira automaticamente em 48 horas.",
    }


@app.delete("/api/reports/share/{share_token}")
def revogar_relatorio_compartilhado(
    share_token: str,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
):
    require_report_upload_token(credentials, settings)
    try:
        revoke_report(share_token, settings)
    except ReportStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"success": True, "message": "Link compartilhado revogado."}


@app.get("/relatorios/{share_token}", include_in_schema=False)
def redirecionar_relatorio_compartilhado(share_token: str):
    # A barra final preserva a resolução dos recursos relativos ./assets/* e
    # ./report-data.json dentro do relatório estático.
    return RedirectResponse(url=f"/relatorios/{share_token}/", status_code=307)


@app.get("/relatorios/{share_token}/", include_in_schema=False)
def abrir_relatorio_compartilhado(share_token: str, request: Request):
    return recurso_relatorio_compartilhado(share_token, "index.html", request)


@app.get("/relatorios/{share_token}/{resource_path:path}", include_in_schema=False)
def recurso_relatorio_compartilhado(
    share_token: str,
    resource_path: str,
    request: Request,
):
    try:
        content, media_type, _payload = read_report_resource(
            share_token,
            resource_path,
            settings,
            public_base_url=_report_public_base_url(request),
        )
    except HTTPException as exc:
        if exc.status_code == 410:
            return _report_link_error_page(
                "Link expirado",
                "Este relatório deixou de ficar disponível porque o período de 48 horas foi encerrado.",
                status_code=410,
            )
        if exc.status_code == 404:
            return _report_link_error_page(
                "Relatório indisponível",
                "O link é inválido, foi revogado ou o relatório já não está disponível.",
                status_code=404,
            )
        raise
    except ReportStoreUnavailable:
        return _report_link_error_page(
            "Serviço temporariamente indisponível",
            "Não foi possível carregar o relatório agora. Tente novamente em alguns instantes.",
            status_code=503,
        )
    return Response(content=content, media_type=media_type)


"""Autenticação e validações de segurança."""
from __future__ import annotations

import secrets
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from .settings import Settings


def require_api_token(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> None:
    expected = settings.api_secret_token
    received = credentials.credentials if credentials is not None else ""
    if not expected or not received or not secrets.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Token inválido ou não fornecido")


def validate_fracttal_bridge_url(url: str, settings: Settings) -> None:
    """Mantém o bridge legado restrito ao domínio oficial do Fracttal.

    O ERP usa URLs diferentes dentro de ``/api``. A validação bloqueia apenas
    hosts externos, esquemas inseguros e caminhos fora da API.
    """
    parsed = urlparse(url)
    base = urlparse(settings.fracttal_base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or not parsed.path.startswith("/api/")
    ):
        raise HTTPException(
            status_code=400,
            detail="O bridge aceita somente URLs HTTPS da API oficial do Fracttal.",
        )


def host_is_allowed(host: str | None, allowed_hosts: tuple[str, ...]) -> bool:
    if not host:
        return False
    normalized = host.lower().rstrip(".")
    for rule in allowed_hosts:
        rule = rule.lower().rstrip(".")
        if rule.startswith("."):
            if normalized.endswith(rule):
                return True
        elif normalized == rule:
            return True
    return False


def require_report_upload_token(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> None:
    """Autoriza somente a publicação/revogação de relatórios.

    Um token dedicado pode ser configurado em ``REPORT_UPLOAD_TOKEN``. A
    credencial principal ``API_SECRET_TOKEN`` também permanece aceita para que
    o ERP não dependa de uma segunda configuração. Nenhum desses valores é
    inserido no relatório ou no link público.
    """
    expected_tokens = tuple(
        token
        for token in (settings.report_upload_token, settings.api_secret_token)
        if token
    )
    received = credentials.credentials if credentials is not None else ""
    authorized = bool(received) and any(
        secrets.compare_digest(received, expected)
        for expected in expected_tokens
    )
    if not authorized:
        raise HTTPException(status_code=401, detail="Token de publicação inválido ou não fornecido")

"""Compartilhamento temporário e público dos relatórios CENTRAL ANAYTICS.

O link público carrega somente um token aleatório assinado. Credenciais do ERP,
do Fracttal e do armazenamento permanecem exclusivamente na API central.
"""
from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import hmac
import json
import mimetypes
from pathlib import Path, PurePosixPath
import secrets
import time
from typing import Mapping, Protocol
import zipfile

from fastapi import HTTPException

from .settings import Settings


REPORT_SHARE_TTL_HOURS = 48
REPORT_SHARE_TTL_SECONDS = REPORT_SHARE_TTL_HOURS * 3600
_REQUIRED_ENTRIES = {
    "index.html",
    "report-data.json",
    "assets/app.js",
    "assets/app.css",
}
_ALLOWED_SUFFIXES = {
    ".html",
    ".json",
    ".js",
    ".css",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
}


class ReportStoreUnavailable(RuntimeError):
    """O armazenamento privado não está configurado ou está indisponível."""


class ReportBundleInvalid(ValueError):
    """O pacote enviado não corresponde a um relatório seguro e navegável."""


class ReportObjectNotFound(FileNotFoundError):
    """O pacote não existe mais no armazenamento privado."""


@dataclass(frozen=True, slots=True)
class ShareTokenPayload:
    object_id: str
    created_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class PublishedReport:
    token: str
    public_url: str
    created_at: datetime
    expires_at: datetime
    valid_hours: int = REPORT_SHARE_TTL_HOURS


class ReportStore(Protocol):
    def put(self, object_key: str, data: bytes, metadata: Mapping[str, str]) -> None: ...

    def get(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...


def _b64encode(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)


def _signing_secret(settings: Settings) -> bytes:
    secret = settings.report_share_signing_secret or settings.api_secret_token
    if not secret or len(secret) < 24:
        raise ReportStoreUnavailable(
            "Configure REPORT_SHARE_SIGNING_SECRET com ao menos 24 caracteres "
            "(ou mantenha API_SECRET_TOKEN configurado) antes de compartilhar relatórios."
        )
    return secret.encode("utf-8")


def issue_share_token(payload: ShareTokenPayload, settings: Settings) -> str:
    serialized = json.dumps(
        {"id": payload.object_id, "iat": payload.created_at, "exp": payload.expires_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _b64encode(serialized)
    signature = _b64encode(hmac.new(_signing_secret(settings), encoded.encode("ascii"), sha256).digest())
    return f"{encoded}.{signature}"


def parse_share_token(
    token: str,
    settings: Settings,
    *,
    now: int | None = None,
    allow_expired: bool = False,
) -> ShareTokenPayload:
    value = str(token or "").strip()
    if not value or len(value) > 768 or value.count(".") != 1:
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado")
    encoded, received_signature = value.split(".", 1)
    expected_signature = _b64encode(
        hmac.new(_signing_secret(settings), encoded.encode("ascii"), sha256).digest()
    )
    if not secrets.compare_digest(received_signature, expected_signature):
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado")
    try:
        raw = json.loads(_b64decode(encoded).decode("utf-8"))
        payload = ShareTokenPayload(
            object_id=str(raw["id"]),
            created_at=int(raw["iat"]),
            expires_at=int(raw["exp"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado") from None
    if not payload.object_id or len(payload.object_id) > 128:
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado")
    current = int(time.time()) if now is None else int(now)
    if payload.expires_at <= payload.created_at or payload.expires_at - payload.created_at != REPORT_SHARE_TTL_SECONDS:
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado")
    if not allow_expired and current >= payload.expires_at:
        raise HTTPException(status_code=410, detail="Este link temporário expirou após 48 horas")
    return payload


def report_object_key(object_id: str) -> str:
    return f"reports/{object_id}.zip"


def _normalized_zip_name(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ReportBundleInvalid("O pacote contém um caminho de arquivo inválido.")
    return path.as_posix()


def validate_report_bundle(bundle: bytes, settings: Settings) -> None:
    if not bundle:
        raise ReportBundleInvalid("O pacote do relatório está vazio.")
    if len(bundle) > settings.report_share_max_bundle_bytes:
        raise ReportBundleInvalid(
            f"O pacote excede o limite de {settings.report_share_max_bundle_bytes // (1024 * 1024)} MB."
        )
    try:
        archive = zipfile.ZipFile(BytesIO(bundle), "r")
    except zipfile.BadZipFile as exc:
        raise ReportBundleInvalid("O arquivo enviado não é um ZIP válido.") from exc

    names: set[str] = set()
    total_uncompressed = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > settings.report_share_max_entries:
            raise ReportBundleInvalid("O pacote contém arquivos demais.")
        for info in infos:
            if info.is_dir():
                continue
            name = _normalized_zip_name(info.filename)
            suffix = PurePosixPath(name).suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES:
                raise ReportBundleInvalid(f"Tipo de arquivo não permitido no relatório: {name}")
            # Bloqueia links simbólicos dentro do ZIP.
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ReportBundleInvalid("O pacote não pode conter links simbólicos.")
            total_uncompressed += int(info.file_size)
            if total_uncompressed > settings.report_share_max_uncompressed_bytes:
                raise ReportBundleInvalid("O conteúdo descompactado excede o limite de segurança.")
            names.add(name)
        missing = _REQUIRED_ENTRIES - names
        if missing:
            raise ReportBundleInvalid(
                "O relatório está incompleto: " + ", ".join(sorted(missing))
            )
        try:
            report_data = json.loads(archive.read("report-data.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportBundleInvalid("O report-data.json do relatório é inválido.") from exc
        if not isinstance(report_data, dict) or not isinstance(report_data.get("metadata"), dict):
            raise ReportBundleInvalid("O pacote não contém metadados válidos do CENTRAL ANAYTICS.")


class LocalReportStore:
    """Armazenamento exclusivo para desenvolvimento e testes locais."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        relative = PurePosixPath(object_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ReportStoreUnavailable("Chave de armazenamento inválida.")
        target = self.root.joinpath(*relative.parts).resolve()
        if self.root not in target.parents and target != self.root:
            raise ReportStoreUnavailable("Chave de armazenamento inválida.")
        return target

    def put(self, object_key: str, data: bytes, metadata: Mapping[str, str]) -> None:
        target = self._path(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        target.with_suffix(target.suffix + ".metadata.json").write_text(
            json.dumps(dict(metadata), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, object_key: str) -> bytes:
        target = self._path(object_key)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise ReportObjectNotFound(object_key) from exc

    def delete(self, object_key: str) -> None:
        target = self._path(object_key)
        target.unlink(missing_ok=True)
        target.with_suffix(target.suffix + ".metadata.json").unlink(missing_ok=True)


class S3ReportStore:
    """Bucket privado S3/R2; nenhuma credencial é devolvida ao navegador."""

    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - protegido pelo requirements
            raise ReportStoreUnavailable("A dependência boto3 não está instalada na API central.") from exc

        required = {
            "REPORT_SHARE_STORAGE_BUCKET": settings.report_share_storage_bucket,
            "REPORT_SHARE_STORAGE_ACCESS_KEY_ID": settings.report_share_storage_access_key_id,
            "REPORT_SHARE_STORAGE_SECRET_ACCESS_KEY": settings.report_share_storage_secret_access_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ReportStoreUnavailable(
                "Armazenamento privado não configurado: " + ", ".join(missing)
            )
        self.bucket = str(settings.report_share_storage_bucket)
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.report_share_storage_endpoint_url or None,
            aws_access_key_id=settings.report_share_storage_access_key_id,
            aws_secret_access_key=settings.report_share_storage_secret_access_key,
            region_name=settings.report_share_storage_region or "auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def put(self, object_key: str, data: bytes, metadata: Mapping[str, str]) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=data,
                ContentType="application/zip",
                CacheControl="no-store",
                Metadata={str(key): str(value) for key, value in metadata.items()},
            )
        except Exception as exc:  # boto3 usa uma hierarquia extensa de exceções
            raise ReportStoreUnavailable("Não foi possível gravar o relatório no armazenamento privado.") from exc

    def get(self, object_key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
            return response["Body"].read()
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise ReportObjectNotFound(object_key) from exc
            raise ReportStoreUnavailable("O armazenamento privado do relatório está indisponível.") from exc

    def delete(self, object_key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise ReportStoreUnavailable("Não foi possível revogar o relatório compartilhado.") from exc


@lru_cache(maxsize=8)
def build_report_store(settings: Settings) -> ReportStore:
    backend = settings.report_share_storage_backend.strip().lower()
    if backend == "local":
        return LocalReportStore(Path(settings.report_share_local_dir))
    if backend in {"r2", "s3"}:
        return S3ReportStore(settings)
    raise ReportStoreUnavailable(
        "REPORT_SHARE_STORAGE_BACKEND deve ser 'r2', 's3' ou 'local'."
    )


def publish_report(
    bundle: bytes,
    settings: Settings,
    *,
    public_base_url: str,
    report_name: str,
    report_period: str,
    now: int | None = None,
) -> PublishedReport:
    validate_report_bundle(bundle, settings)
    current = int(time.time()) if now is None else int(now)
    expires = current + REPORT_SHARE_TTL_SECONDS
    object_id = secrets.token_urlsafe(24)
    payload = ShareTokenPayload(object_id=object_id, created_at=current, expires_at=expires)
    token = issue_share_token(payload, settings)
    store = build_report_store(settings)
    store.put(
        report_object_key(object_id),
        bundle,
        {
            "created-at": str(current),
            "expires-at": str(expires),
            "valid-hours": str(REPORT_SHARE_TTL_HOURS),
            "report-name-sha256": sha256(report_name.encode("utf-8")).hexdigest(),
            "report-period-sha256": sha256(report_period.encode("utf-8")).hexdigest(),
        },
    )
    base = public_base_url.rstrip("/")
    return PublishedReport(
        token=token,
        public_url=f"{base}/relatorios/{token}/",
        created_at=datetime.fromtimestamp(current, tz=timezone.utc),
        expires_at=datetime.fromtimestamp(expires, tz=timezone.utc),
    )


def revoke_report(token: str, settings: Settings) -> None:
    payload = parse_share_token(token, settings, allow_expired=True)
    build_report_store(settings).delete(report_object_key(payload.object_id))


def _safe_resource_path(resource_path: str | None) -> str:
    value = (resource_path or "index.html").strip().replace("\\", "/").lstrip("/")
    if not value:
        value = "index.html"
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=404, detail="Recurso não encontrado")
    normalized = path.as_posix()
    if PurePosixPath(normalized).suffix.lower() not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=404, detail="Recurso não encontrado")
    return normalized


def _sharing_payload(token: str, payload: ShareTokenPayload, public_url: str) -> dict[str, object]:
    return {
        "mode": "shared",
        "validHours": REPORT_SHARE_TTL_HOURS,
        "publicAccess": True,
        "requiresCredentials": False,
        "sharedAt": datetime.fromtimestamp(payload.created_at, tz=timezone.utc).isoformat(),
        "expiresAt": datetime.fromtimestamp(payload.expires_at, tz=timezone.utc).isoformat(),
        "publicUrl": public_url,
        "notice": (
            "Link temporário compartilhado sem solicitação de credenciais. "
            "O acesso expira automaticamente 48 horas após a publicação."
        ),
    }


def read_report_resource(
    token: str,
    resource_path: str | None,
    settings: Settings,
    *,
    public_base_url: str,
    now: int | None = None,
) -> tuple[bytes, str, ShareTokenPayload]:
    try:
        payload = parse_share_token(token, settings, now=now)
    except HTTPException as exc:
        if exc.status_code == 410:
            try:
                expired_payload = parse_share_token(token, settings, now=now, allow_expired=True)
                build_report_store(settings).delete(report_object_key(expired_payload.object_id))
            except Exception:
                pass
        raise

    resource = _safe_resource_path(resource_path)
    try:
        bundle = build_report_store(settings).get(report_object_key(payload.object_id))
    except ReportObjectNotFound:
        raise HTTPException(status_code=404, detail="Link de relatório inválido ou expirado") from None
    try:
        with zipfile.ZipFile(BytesIO(bundle), "r") as archive:
            content = archive.read(resource)
    except (zipfile.BadZipFile, KeyError):
        raise HTTPException(status_code=404, detail="Recurso não encontrado") from None

    if resource == "report-data.json":
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=500, detail="Dados do relatório indisponíveis") from None
        public_url = f"{public_base_url.rstrip('/')}/relatorios/{token}/"
        data["sharing"] = _sharing_payload(token, payload, public_url)
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    media_type = mimetypes.guess_type(resource)[0] or "application/octet-stream"
    if resource.endswith(".js"):
        media_type = "text/javascript"
    return content, media_type, payload

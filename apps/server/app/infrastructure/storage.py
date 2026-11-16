from __future__ import annotations

import logging
import re
from pathlib import Path
import shutil
from urllib.parse import quote, unquote, urlparse

from app.core import config

logger = logging.getLogger(__name__)
_PRESIGNED_URL_TTL_SECONDS = 7 * 24 * 60 * 60


class ObjectStorageError(RuntimeError):
    """Object storage is unavailable or failed to publish an object."""


_STORAGE_PREFIX = "storage://"


def is_configured() -> bool:
    return all((
        config.S3_ENDPOINT_URL,
        config.S3_BUCKET,
        config.S3_ACCESS_KEY_ID,
        config.S3_SECRET_ACCESS_KEY,
    ))


def _client():
    if not is_configured():
        raise ObjectStorageError("请先配置 S3/OSS 对象存储")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ObjectStorageError("未安装 boto3，请安装后端 requirements") from exc

    endpoint_region = re.search(r"oss-([a-z0-9-]+)\.", config.S3_ENDPOINT_URL)
    region = config.S3_REGION or (endpoint_region.group(1) if endpoint_region else "us-east-1")
    hostname = (urlparse(config.S3_ENDPOINT_URL).hostname or "").lower()
    # OSS requires the bucket in the hostname; keep path addressing for
    # other S3-compatible endpoints such as local MinIO.
    addressing_style = "virtual" if hostname.endswith(".aliyuncs.com") else "path"
    checksum_options = {}
    if addressing_style == "virtual":
        # OSS's S3 API does not implement the SDK's optional aws-chunked
        # STREAMING-UNSIGNED-PAYLOAD-TRAILER checksum encoding.
        checksum_options = {
            "request_checksum_calculation": "when_required",
            "response_checksum_validation": "when_required",
        }

    return boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT_URL,
        aws_access_key_id=config.S3_ACCESS_KEY_ID,
        aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
        region_name=region,
        config=Config(
            signature_version="s3v4", s3={"addressing_style": addressing_style},
            **checksum_options,
        ),
    )


def upload_file(file_path: Path, object_key: str, content_type: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise ObjectStorageError(f"待上传文件不存在：{path.name}")

    client = _client()
    try:
        client.upload_file(
            str(path),
            config.S3_BUCKET,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )
        if config.S3_PUBLIC_BASE_URL:
            return f"{config.S3_PUBLIC_BASE_URL}/{quote(object_key, safe='/')}"
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.S3_BUCKET, "Key": object_key},
            ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
        )
    except Exception as exc:
        logger.exception("event=object_storage.upload_failed key=%s", object_key)
        raise ObjectStorageError(f"上传 OSS 失败：{Path(object_key).name}") from exc


def object_key_from_reference(reference: str) -> str | None:
    if reference.startswith(_STORAGE_PREFIX):
        return unquote(reference[len(_STORAGE_PREFIX):])
    public_base = config.S3_PUBLIC_BASE_URL
    if public_base and reference.startswith(f"{public_base}/"):
        return unquote(reference[len(public_base) + 1:].split("?", 1)[0])
    if reference.startswith(("http://", "https://")):
        parts = unquote(urlparse(reference).path.lstrip("/")).split("/")
        if parts and parts[0] == config.S3_BUCKET:
            return "/".join(parts[1:])
        return "/".join(parts)
    if reference.startswith("assets/") or reference.startswith(("uploads/", "previews/", "exports/")):
        return reference
    return None


def persist_file(source: Path, object_key: str, content_type: str) -> str:
    """Store a local staging file and return a portable reference."""
    source = Path(source)
    if is_configured():
        upload_file(source, object_key, content_type)
        return f"{_STORAGE_PREFIX}{quote(object_key, safe='/')}"
    return str(source)


def download_reference(reference: str, destination: Path) -> Path:
    """Materialize either a portable OSS reference or a legacy local path."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = object_key_from_reference(reference)
    if key:
        download_file(key, destination)
        return destination
    source = Path(reference)
    if not source.is_file():
        raise ObjectStorageError(f"找不到存储文件：{source.name}")
    shutil.copyfile(source, destination)
    return destination


def local_path(reference: str) -> Path | None:
    if object_key_from_reference(reference):
        return None
    return Path(reference)


def delete_reference(reference: str) -> None:
    key = object_key_from_reference(reference)
    if key and is_configured():
        try:
            _client().delete_object(Bucket=config.S3_BUCKET, Key=key)
        except Exception as exc:
            logger.exception("event=object_storage.delete_failed key=%s", key)
            raise ObjectStorageError("删除对象存储文件失败") from exc
        return
    path = Path(reference)
    path.unlink(missing_ok=True)


def access_url(reference: str, expires_seconds: int = _PRESIGNED_URL_TTL_SECONDS) -> str:
    key = object_key_from_reference(reference)
    if not key:
        return reference
    if config.S3_PUBLIC_BASE_URL:
        return f"{config.S3_PUBLIC_BASE_URL}/{quote(key, safe='/')}"
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": config.S3_BUCKET, "Key": key},
            ExpiresIn=expires_seconds,
        )
    except Exception as exc:
        logger.exception("event=object_storage.sign_failed key=%s", key)
        raise ObjectStorageError("无法生成临时文件访问地址") from exc


def download_file(object_key: str, destination: Path) -> None:
    client = _client()
    try:
        client.download_file(config.S3_BUCKET, object_key, str(destination))
    except Exception as exc:
        logger.exception("event=object_storage.download_failed key=%s", object_key)
        raise ObjectStorageError(f"下载 OSS 文件失败：{Path(object_key).name}") from exc


def helper_url(draft_url: str) -> str:
    helper_base = config.JIANYING_HELPER_URL.rstrip("?")
    separator = "&" if "?" in helper_base.partition("#")[2] else "?"
    return f"{helper_base}{separator}drafId={quote(draft_url, safe='')}"

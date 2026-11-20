"""Private application files stored on disk or in an S3-compatible bucket."""
from contextlib import closing
from functools import lru_cache
import logging
import os
from pathlib import PurePosixPath

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from . import db


_REMOTE = "s3:"
_logger = logging.getLogger(__name__)


def local_path(folder: str, name: str):
    root = (db.DATA_DIR / folder).resolve()
    path = (root / name).resolve()
    if path.parent != root or any(sep in name for sep in ("/", "\\", ":")):
        raise HTTPException(400, "文件路径无效")
    return path


@lru_cache(maxsize=4)
def _s3_client(endpoint_url: str | None, region: str | None):
    try:
        from boto3.session import Session
        from botocore.config import Config
    except ImportError:
        raise RuntimeError("S3 对象存储需要安装 boto3") from None
    return Session().client(
        "s3", endpoint_url=endpoint_url, region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"mode": "standard", "total_max_attempts": 3},
            s3={"addressing_style": "path"} if endpoint_url else None,
        ),
    )


def _s3_request(operation: str, key: str, **params):
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if not bucket:
        raise HTTPException(503, "对象存储未配置")
    try:
        endpoint = os.environ.get("S3_ENDPOINT_URL") or None
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        client = _s3_client(endpoint, region or ("us-east-1" if endpoint else None))
        return getattr(client, operation)(Bucket=bucket, Key=key, **params)
    except Exception as exc:
        error = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if operation == "get_object" and error in {"NoSuchKey", "NotFound", "404"}:
            raise HTTPException(404, "文件不存在") from None
        _logger.warning("Object storage %s failed (%s)", operation, error or type(exc).__name__)
        raise HTTPException(503, "对象存储暂时不可用") from None


def _remote_key(reference: str) -> str:
    key = reference[len(_REMOTE):]
    if not key or key.startswith("/") or "\\" in key or ".." in PurePosixPath(key).parts:
        raise HTTPException(400, "文件路径无效")
    return key


def save_file(folder: str, name: str, content: bytes, media_type: str = "application/octet-stream") -> str:
    path = local_path(folder, name)
    if os.environ.get("S3_BUCKET", "").strip():
        prefix = os.environ.get("S3_PREFIX", "").strip("/")
        reference = _REMOTE + "/".join(part for part in (prefix, folder, name) if part)
        _s3_request("put_object", _remote_key(reference), Body=content, ContentType=media_type)
        return reference
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return name


def read_file(folder: str, reference: str) -> bytes:
    if reference.startswith(_REMOTE):
        body = _s3_request("get_object", _remote_key(reference))["Body"]
        try:
            with closing(body):
                return body.read()
        except Exception as exc:
            _logger.warning("Object storage body read failed (%s)", type(exc).__name__)
            raise HTTPException(503, "对象存储暂时不可用") from None
    try:
        return local_path(folder, reference).read_bytes()
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在") from None


def delete_file(folder: str, reference: str) -> None:
    if reference.startswith(_REMOTE):
        _s3_request("delete_object", _remote_key(reference))
    else:
        local_path(folder, reference).unlink(missing_ok=True)


def discard_file(folder: str, reference: str) -> None:
    """Best-effort cleanup while propagating an upload's original error."""
    try:
        delete_file(folder, reference)
    except (OSError, HTTPException):
        _logger.warning("Could not remove an uncommitted %s file", folder)


def file_response(folder: str, reference: str, *, media_type: str, headers: dict[str, str]):
    if reference.startswith(_REMOTE):
        return Response(read_file(folder, reference), media_type=media_type, headers=headers)
    path = local_path(folder, reference)
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, media_type=media_type, headers=headers)

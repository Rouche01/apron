"""Blob backends for RecordingStore — local disk or S3-compatible (R2/S3)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("computer.gateway.recording.blob")


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def delete_key(self, key: str) -> None: ...

    def delete_prefix(self, prefix: str) -> None: ...

    def list_keys(self, prefix: str) -> list[tuple[str, int]]:
        """Return (key, size_bytes) under prefix."""
        ...


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        key = key.lstrip("/")
        if ".." in key.split("/"):
            raise ValueError(f"invalid blob key: {key!r}")
        return self.root / key

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def delete_key(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.lstrip("/")
        base = self._path(prefix) if prefix else self.root
        if base.is_file():
            base.unlink(missing_ok=True)
            return
        if not base.is_dir():
            # prefix may be "t/acme/sid/" with no dir yet
            return
        import shutil

        shutil.rmtree(base, ignore_errors=True)

    def list_keys(self, prefix: str) -> list[tuple[str, int]]:
        prefix = prefix.lstrip("/")
        root = self.root
        out: list[tuple[str, int]] = []
        if not root.is_dir():
            return out
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if prefix and not rel.startswith(prefix):
                continue
            out.append((rel, path.stat().st_size))
        return out


class S3BlobStore:
    """S3-compatible object store (AWS S3, Cloudflare R2, MinIO, …)."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError(
                "RECORDING_BACKEND=s3 requires boto3 — pip install boto3"
            ) from e
        self.bucket = bucket
        self.prefix = prefix.strip().strip("/")
        kwargs: dict = {"config": Config(signature_version="s3v4")}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if region:
            kwargs["region_name"] = region
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self._client = boto3.client("s3", **kwargs)

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            ContentType=content_type,
        )

    def get(self, key: str) -> bytes | None:
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            ClientError = Exception  # type: ignore[misc, assignment]
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return obj["Body"].read()

    def delete_key(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception:
            logger.debug("s3 delete_key failed key=%s", key, exc_info=True)

    def delete_prefix(self, prefix: str) -> None:
        full = self._key(prefix.lstrip("/"))
        if full and not full.endswith("/"):
            full = full + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            contents = page.get("Contents") or []
            if not contents:
                continue
            self._client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
            )

    def list_keys(self, prefix: str) -> list[tuple[str, int]]:
        full = self._key(prefix.lstrip("/"))
        out: list[tuple[str, int]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        strip = f"{self.prefix}/" if self.prefix else ""
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents") or []:
                k = obj["Key"]
                if strip and k.startswith(strip):
                    k = k[len(strip) :]
                out.append((k, int(obj.get("Size") or 0)))
        return out

    @classmethod
    def from_env(cls) -> S3BlobStore:
        bucket = os.environ.get("RECORDING_S3_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError("RECORDING_S3_BUCKET is required for RECORDING_BACKEND=s3")
        return cls(
            bucket=bucket,
            prefix=os.environ.get("RECORDING_S3_PREFIX", "recordings").strip(),
            endpoint_url=os.environ.get("RECORDING_S3_ENDPOINT", "").strip() or None,
            region=os.environ.get("RECORDING_S3_REGION", "").strip() or None,
            access_key=os.environ.get("RECORDING_S3_ACCESS_KEY", "").strip() or None,
            secret_key=os.environ.get("RECORDING_S3_SECRET_KEY", "").strip() or None,
        )


def build_blob_store(*, local_root: Path | None = None) -> BlobStore:
    backend = os.environ.get("RECORDING_BACKEND", "local").strip().lower()
    if backend in ("s3", "r2", "object", "minio"):
        store = S3BlobStore.from_env()
        logger.info(
            "recording blob backend=s3 bucket=%s prefix=%s",
            store.bucket,
            store.prefix or "-",
        )
        return store
    root = local_root
    if root is None:
        env_root = os.environ.get("RECORDING_DIR", "").strip()
        preferred = Path(env_root or "/data/recordings")
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            root = preferred
        except OSError:
            root = Path.cwd() / ".data" / "recordings"
            root.mkdir(parents=True, exist_ok=True)
            logger.warning("RECORDING_DIR %s not writable; using %s", preferred, root)
    else:
        root.mkdir(parents=True, exist_ok=True)
    logger.info("recording blob backend=local root=%s", root)
    return LocalBlobStore(root)

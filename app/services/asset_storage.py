from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AssetStorage(Protocol):
    def put_bytes(self, content: bytes, *, suffix: str, namespace: str) -> tuple[str, str]:
        ...

    def public_url(self, storage_key: str) -> str:
        ...


@dataclass
class LocalAssetStorage:
    root: Path
    public_prefix: str = "/media"

    def put_bytes(self, content: bytes, *, suffix: str, namespace: str) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        rel = Path(namespace) / digest[:2] / f"{digest}{suffix}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        return rel.as_posix(), digest

    def public_url(self, storage_key: str) -> str:
        return f"{self.public_prefix.rstrip('/')}/{storage_key.lstrip('/')}"


@dataclass
class S3CompatibleAssetStorage:
    bucket: str
    public_base_url: str

    def put_bytes(self, content: bytes, *, suffix: str, namespace: str) -> tuple[str, str]:
        import boto3  # type: ignore

        digest = hashlib.sha256(content).hexdigest()
        key = f"{namespace}/{digest[:2]}/{digest}{suffix}"
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("ASSET_S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.getenv("ASSET_S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("ASSET_S3_SECRET_ACCESS_KEY") or None,
            region_name=os.getenv("ASSET_S3_REGION") or None,
        )
        client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType="image/png")
        return key, digest

    def public_url(self, storage_key: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/{storage_key.lstrip('/')}"


def get_asset_storage() -> AssetStorage:
    backend = os.getenv("ASSET_STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        bucket = os.getenv("ASSET_S3_BUCKET", "").strip()
        base_url = os.getenv("ASSET_PUBLIC_BASE_URL", "").strip()
        if not bucket or not base_url:
            raise RuntimeError("ASSET_STORAGE_BACKEND=s3 requires ASSET_S3_BUCKET and ASSET_PUBLIC_BASE_URL")
        return S3CompatibleAssetStorage(bucket=bucket, public_base_url=base_url)
    root = Path(os.getenv("ASSET_LOCAL_DIR", "media_uploads"))
    return LocalAssetStorage(root=root)

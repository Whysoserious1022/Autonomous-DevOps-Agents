"""
cascade/storage/backends/s3.py
────────────────────────────────
S3 / MinIO artifact store backend (Phase 4).

Compatible with AWS S3 and MinIO (via endpoint_url override).
Lazy-imports boto3 so the core package works without it.
"""

from __future__ import annotations

from typing import Any

from cascade.storage.artifact_store import ArtifactStore, make_uri, parse_uri


class S3ArtifactStore(ArtifactStore):
    """
    S3/MinIO content-addressed artifact store.

    All artifacts are stored under key: artifacts/<prefix2>/<sha256_hash>
    (mirrors the LocalArtifactStore shard structure).
    """

    KEY_PREFIX = "artifacts"

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        try:
            import boto3  # noqa: PLC0415
        except ImportError as e:
            msg = "boto3 is required for S3 backend. Install with: pip install cascade[s3]"
            raise ImportError(msg) from e

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
        )
        # Ensure bucket exists (MinIO auto-creates, S3 requires explicit create)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001
            self._client.create_bucket(Bucket=self.bucket)

    def _key(self, content_hash: str) -> str:
        return f"{self.KEY_PREFIX}/{content_hash[:2]}/{content_hash}"

    def put_bytes(self, data: bytes) -> str:
        content_hash = self.content_hash(data)
        key = self._key(content_hash)
        # Check existence first to avoid unnecessary PUT
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return make_uri(content_hash)

    def get_bytes(self, uri: str) -> bytes:
        content_hash = parse_uri(uri)
        key = self._key(content_hash)
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except self._client.exceptions.NoSuchKey as e:
            msg = f"Artifact not found in S3: {uri}"
            raise KeyError(msg) from e

    def exists(self, uri: str) -> bool:
        try:
            content_hash = parse_uri(uri)
            self._client.head_object(Bucket=self.bucket, Key=self._key(content_hash))
            return True
        except Exception:  # noqa: BLE001
            return False

    def __repr__(self) -> str:
        return f"S3ArtifactStore(bucket={self.bucket!r})"

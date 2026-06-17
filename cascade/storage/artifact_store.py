"""
cascade/storage/artifact_store.py
──────────────────────────────────
Abstract ArtifactStore interface + factory.

Content-Addressed Storage (CAS):
  All artifacts are stored under their SHA-256 hash.
  URI format: sha256://<hex_digest>
  
  This means:
  - Duplicate blobs are automatically deduplicated.
  - Artifacts are immutable — you never overwrite by the same hash.
  - The same RepoGraph from two different runs shares one on-disk copy.
"""

from __future__ import annotations

import hashlib
import pickle
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import orjson


# ── URI Constants ─────────────────────────────────────────────────────────────

CAS_SCHEME = "sha256"


def make_uri(content_hash: str) -> str:
    return f"{CAS_SCHEME}://{content_hash}"


def parse_uri(uri: str) -> str:
    """Extract hex digest from a sha256:// URI."""
    if not uri.startswith(f"{CAS_SCHEME}://"):
        msg = f"Invalid CAS URI: '{uri}'. Expected format: sha256://<hex_digest>"
        raise ValueError(msg)
    return uri[len(f"{CAS_SCHEME}://"):]


# ── Abstract Interface ─────────────────────────────────────────────────────────

class ArtifactStore(ABC):
    """
    Abstract content-addressed artifact store.

    All implementations must be idempotent: storing the same content
    twice is a no-op (same hash → same URI).
    """

    # ── Core byte-level operations ────────────────────────────────────────────

    @abstractmethod
    def put_bytes(self, data: bytes) -> str:
        """
        Store raw bytes. Returns the CAS URI (sha256://<hash>).
        Idempotent: calling twice with same bytes returns same URI.
        """
        ...

    @abstractmethod
    def get_bytes(self, uri: str) -> bytes:
        """Retrieve raw bytes by CAS URI. Raises KeyError if not found."""
        ...

    @abstractmethod
    def exists(self, uri: str) -> bool:
        """Return True if the artifact exists in the store."""
        ...

    # ── Typed helpers ──────────────────────────────────────────────────────────

    def put_json(self, obj: Any) -> str:
        """Serialize obj to JSON (via orjson) and store. Returns CAS URI."""
        data = orjson.dumps(obj, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        return self.put_bytes(data)

    def get_json(self, uri: str) -> Any:
        """Retrieve and deserialize JSON artifact."""
        return orjson.loads(self.get_bytes(uri))

    def put_text(self, text: str, encoding: str = "utf-8") -> str:
        """Store a text string. Returns CAS URI."""
        return self.put_bytes(text.encode(encoding))

    def get_text(self, uri: str, encoding: str = "utf-8") -> str:
        """Retrieve artifact as text string."""
        return self.get_bytes(uri).decode(encoding)

    def put_pickle(self, obj: Any) -> str:
        """Pickle-serialize a Python object and store. Returns CAS URI."""
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        return self.put_bytes(data)

    def get_pickle(self, uri: str) -> Any:
        """Retrieve and unpickle a Python object."""
        return pickle.loads(self.get_bytes(uri))  # noqa: S301 — trusted internal store

    def put_file(self, path: Path) -> str:
        """Store file contents from disk. Returns CAS URI."""
        data = path.read_bytes()
        return self.put_bytes(data)

    def get_file(self, uri: str, dest: Path) -> None:
        """Retrieve artifact and write to dest path."""
        dest.write_bytes(self.get_bytes(uri))

    # ── Hash utility ──────────────────────────────────────────────────────────

    @staticmethod
    def content_hash(data: bytes) -> str:
        """Return SHA-256 hex digest of data."""
        return hashlib.sha256(data).hexdigest()


# ── Local Filesystem Backend ──────────────────────────────────────────────────

class LocalArtifactStore(ArtifactStore):
    """
    Filesystem-based CAS artifact store.

    Layout on disk:
        <root>/
          ab/
            abcdef1234...   ← file named by full SHA-256 digest
          cd/
            cdef5678...
          ...

    The 2-char prefix directory shards the flat namespace into 256 buckets,
    preventing filesystem slowdowns with millions of files (mirrors Git's object store).
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _artifact_path(self, content_hash: str) -> Path:
        """Return the filesystem path for a given content hash."""
        prefix = content_hash[:2]
        shard_dir = self.root / prefix
        shard_dir.mkdir(parents=True, exist_ok=True)
        return shard_dir / content_hash

    def put_bytes(self, data: bytes) -> str:
        content_hash = self.content_hash(data)
        path = self._artifact_path(content_hash)
        if not path.exists():
            # Atomic write: write to temp then rename (prevents partial reads)
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_bytes(data)
            tmp_path.rename(path)
        return make_uri(content_hash)

    def get_bytes(self, uri: str) -> bytes:
        content_hash = parse_uri(uri)
        path = self._artifact_path(content_hash)
        if not path.exists():
            msg = f"Artifact not found in local store: {uri}"
            raise KeyError(msg)
        return path.read_bytes()

    def exists(self, uri: str) -> bool:
        try:
            content_hash = parse_uri(uri)
        except ValueError:
            return False
        return self._artifact_path(content_hash).exists()

    def list_all(self) -> list[str]:
        """Return all artifact URIs in the store (for debugging)."""
        uris = []
        for shard in self.root.iterdir():
            if shard.is_dir() and len(shard.name) == 2:
                for artifact in shard.iterdir():
                    if artifact.is_file() and not artifact.suffix:
                        uris.append(make_uri(artifact.name))
        return sorted(uris)

    def total_size_bytes(self) -> int:
        """Total disk usage of the artifact store."""
        return sum(
            f.stat().st_size
            for f in self.root.rglob("*")
            if f.is_file() and not f.suffix
        )

    def __repr__(self) -> str:
        return f"LocalArtifactStore(root={self.root!r})"


# ── Store Factory ─────────────────────────────────────────────────────────────

def create_artifact_store(backend: str = "local", **kwargs: Any) -> ArtifactStore:
    """
    Factory function for creating artifact store instances.

    Args:
        backend: "local" or "s3"
        **kwargs: Backend-specific kwargs
                  local: root=<path>
                  s3: bucket=<str>, endpoint_url=<str|None>
    """
    if backend == "local":
        root = kwargs.get("root", Path.home() / ".cascade" / "artifacts")
        return LocalArtifactStore(root=root)
    if backend == "s3":
        # Import lazily to avoid requiring boto3 for local-only usage
        from cascade.storage.backends.s3 import S3ArtifactStore  # noqa: PLC0415
        return S3ArtifactStore(
            bucket=kwargs["bucket"],
            endpoint_url=kwargs.get("endpoint_url"),
        )
    msg = f"Unknown artifact backend: '{backend}'. Supported: 'local', 's3'."
    raise ValueError(msg)

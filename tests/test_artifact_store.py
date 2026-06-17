"""
tests/test_artifact_store.py
──────────────────────────────
Tests for the CAS artifact store (LocalArtifactStore).
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest

from cascade.storage.artifact_store import (
    LocalArtifactStore,
    ArtifactStore,
    make_uri,
    parse_uri,
    create_artifact_store,
)


class TestURIHelpers:
    def test_make_uri_format(self):
        uri = make_uri("abcdef1234567890" * 4)
        assert uri.startswith("sha256://")

    def test_parse_uri_extracts_hash(self):
        digest = "a" * 64
        uri = make_uri(digest)
        assert parse_uri(uri) == digest

    def test_parse_invalid_uri_raises(self):
        with pytest.raises(ValueError, match="Invalid CAS URI"):
            parse_uri("s3://some-bucket/file")


class TestLocalArtifactStore:
    def test_put_and_get_bytes(self, artifact_store: LocalArtifactStore):
        data = b"hello cascade artifact"
        uri = artifact_store.put_bytes(data)
        assert artifact_store.get_bytes(uri) == data

    def test_put_is_idempotent(self, artifact_store: LocalArtifactStore):
        data = b"same content"
        uri1 = artifact_store.put_bytes(data)
        uri2 = artifact_store.put_bytes(data)
        assert uri1 == uri2
        # Only one file should exist
        assert len(artifact_store.list_all()) == 1

    def test_exists_true_after_put(self, artifact_store: LocalArtifactStore):
        uri = artifact_store.put_bytes(b"test")
        assert artifact_store.exists(uri)

    def test_exists_false_for_unknown(self, artifact_store: LocalArtifactStore):
        assert not artifact_store.exists(f"sha256://{'0' * 64}")

    def test_get_missing_raises_key_error(self, artifact_store: LocalArtifactStore):
        with pytest.raises(KeyError, match="not found"):
            artifact_store.get_bytes(f"sha256://{'1' * 64}")

    def test_put_and_get_json(self, artifact_store: LocalArtifactStore):
        obj = {"repo": "fastapi", "branches": [1, 2, 3], "nested": {"a": True}}
        uri = artifact_store.put_json(obj)
        result = artifact_store.get_json(uri)
        assert result == obj

    def test_put_and_get_text(self, artifact_store: LocalArtifactStore):
        text = "This is a code diff\n+def hello(): pass\n"
        uri = artifact_store.put_text(text)
        assert artifact_store.get_text(uri) == text

    def test_put_and_get_pickle(self, artifact_store: LocalArtifactStore):
        obj = {"complex": [1, 2, {"nested": True}], "tuple": (1, 2, 3)}
        uri = artifact_store.put_pickle(obj)
        result = artifact_store.get_pickle(uri)
        assert result == obj

    def test_put_and_get_file(self, artifact_store: LocalArtifactStore, tmp_path: Path):
        src = tmp_path / "source.txt"
        src.write_text("file content here")
        uri = artifact_store.put_file(src)

        dest = tmp_path / "dest.txt"
        artifact_store.get_file(uri, dest)
        assert dest.read_text() == "file content here"

    def test_different_content_different_uri(self, artifact_store: LocalArtifactStore):
        uri1 = artifact_store.put_bytes(b"content A")
        uri2 = artifact_store.put_bytes(b"content B")
        assert uri1 != uri2

    def test_shard_directory_structure(self, artifact_store: LocalArtifactStore):
        """Verify 2-char prefix sharding like Git's object store."""
        uri = artifact_store.put_bytes(b"sharding test")
        content_hash = parse_uri(uri)
        shard_dir = artifact_store.root / content_hash[:2]
        artifact_file = shard_dir / content_hash
        assert shard_dir.is_dir()
        assert artifact_file.is_file()

    def test_list_all(self, artifact_store: LocalArtifactStore):
        artifact_store.put_bytes(b"one")
        artifact_store.put_bytes(b"two")
        artifact_store.put_bytes(b"three")
        uris = artifact_store.list_all()
        assert len(uris) == 3
        assert all(u.startswith("sha256://") for u in uris)

    def test_total_size_bytes(self, artifact_store: LocalArtifactStore):
        data = b"x" * 1024
        artifact_store.put_bytes(data)
        size = artifact_store.total_size_bytes()
        assert size >= 1024

    def test_large_data_stored_correctly(self, artifact_store: LocalArtifactStore):
        """Verify multi-MB artifacts store and retrieve correctly."""
        large = b"cascade" * 100_000  # ~700KB
        uri = artifact_store.put_bytes(large)
        assert artifact_store.get_bytes(uri) == large


class TestArtifactStoreFactory:
    def test_create_local_store(self, tmp_path: Path):
        store = create_artifact_store("local", root=tmp_path / "artifacts")
        assert isinstance(store, LocalArtifactStore)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown artifact backend"):
            create_artifact_store("gcs")

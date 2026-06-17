"""
tests/test_s3.py
────────────────
Unit tests for the S3 / MinIO CAS artifact store backend.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from cascade.storage.backends.s3 import S3ArtifactStore
from cascade.storage.artifact_store import make_uri, parse_uri


def test_s3_store_init_bucket_exists():
    """Verify S3 store checks bucket existence and doesn't recreate if it exists."""
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client) as mock_boto:
        store = S3ArtifactStore(bucket="my-bucket")
        mock_boto.assert_called_once_with("s3", endpoint_url=None, region_name="us-east-1")
        mock_client.head_bucket.assert_called_once_with(Bucket="my-bucket")
        mock_client.create_bucket.assert_not_called()


def test_s3_store_init_bucket_missing_creates():
    """Verify S3 store creates the bucket if head_bucket raises an exception."""
    mock_client = MagicMock()
    mock_client.head_bucket.side_effect = Exception("Bucket does not exist")
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="new-bucket")
        mock_client.head_bucket.assert_called_once_with(Bucket="new-bucket")
        mock_client.create_bucket.assert_called_once_with(Bucket="new-bucket")


def test_s3_store_put_bytes():
    """Verify put_bytes uploads to correct key if it doesn't already exist."""
    mock_client = MagicMock()
    # head_object raises to simulate cache miss (needs upload)
    mock_client.head_object.side_effect = Exception("Not found")
    
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="my-bucket")
        data = b"cascade binary payload"
        uri = store.put_bytes(data)
        
        content_hash = store.content_hash(data)
        expected_key = f"artifacts/{content_hash[:2]}/{content_hash}"
        
        assert uri == make_uri(content_hash)
        mock_client.put_object.assert_called_once_with(
            Bucket="my-bucket",
            Key=expected_key,
            Body=data,
        )


def test_s3_store_put_bytes_already_exists():
    """Verify put_bytes skips upload if the object already exists in the bucket."""
    mock_client = MagicMock()
    # head_object returns metadata (simulate object exists)
    mock_client.head_object.return_value = {"ContentLength": 100}
    
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="my-bucket")
        data = b"existing data"
        uri = store.put_bytes(data)
        
        mock_client.head_object.assert_called_once()
        mock_client.put_object.assert_not_called()


def test_s3_store_get_bytes():
    """Verify get_bytes retrieves body from correct key."""
    mock_client = MagicMock()
    mock_body = MagicMock()
    mock_body.read.return_value = b"retrieved payload"
    mock_client.get_object.return_value = {"Body": mock_body}
    
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="my-bucket")
        uri = make_uri("a" * 64)
        
        assert store.get_bytes(uri) == b"retrieved payload"
        expected_key = f"artifacts/aa/{'a' * 64}"
        mock_client.get_object.assert_called_once_with(Bucket="my-bucket", Key=expected_key)


def test_s3_store_get_bytes_missing_raises_key_error():
    """Verify get_bytes raises KeyError when NoSuchKey is encountered."""
    mock_client = MagicMock()
    # Setup mock client exception
    class NoSuchKeyException(Exception):
        pass
    mock_client.exceptions.NoSuchKey = NoSuchKeyException
    mock_client.get_object.side_effect = NoSuchKeyException("The key does not exist")
    
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="my-bucket")
        with pytest.raises(KeyError, match="Artifact not found"):
            store.get_bytes(make_uri("b" * 64))


def test_s3_store_exists():
    """Verify exists returns True/False based on head_object success."""
    mock_client = MagicMock()
    mock_client.head_object.side_effect = [
        {"ContentLength": 12},  # First call succeeds
        Exception("Not found"), # Second call fails
    ]
    
    with patch("boto3.client", return_value=mock_client):
        store = S3ArtifactStore(bucket="my-bucket")
        
        # Test True
        assert store.exists(make_uri("c" * 64))
        
        # Test False
        assert not store.exists(make_uri("d" * 64))

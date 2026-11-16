from app.infrastructure import storage as object_storage


def test_persist_file_returns_portable_reference_when_oss_enabled(monkeypatch, tmp_path):
    calls = {}

    def fake_upload(path, key, content_type):
        calls.update(path=path, key=key, content_type=content_type)
        return "https://bucket.example/key"

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage, "upload_file", fake_upload)
    source = tmp_path / "sample.txt"
    source.write_text("content", encoding="utf-8")

    reference = object_storage.persist_file(source, "uploads/user/workspace/sample.txt", "text/plain")

    assert reference == "storage://uploads/user/workspace/sample.txt"
    assert object_storage.object_key_from_reference(reference) == "uploads/user/workspace/sample.txt"
    assert calls["key"] == "uploads/user/workspace/sample.txt"


def test_download_reference_handles_local_and_oss_paths(monkeypatch, tmp_path):
    source = tmp_path / "old.txt"
    source.write_text("legacy", encoding="utf-8")
    local_destination = tmp_path / "copy.txt"
    object_storage.download_reference(str(source), local_destination)
    assert local_destination.read_text(encoding="utf-8") == "legacy"

    def fake_download(key, destination):
        assert key == "assets/user/workspace/file.png"
        destination.write_bytes(b"image")

    monkeypatch.setattr(object_storage, "download_file", fake_download)
    object_destination = tmp_path / "object.png"
    object_storage.download_reference("storage://assets/user/workspace/file.png", object_destination)
    assert object_destination.read_bytes() == b"image"


def test_oss_region_is_inferred_from_regional_endpoint(monkeypatch):
    class FakeBoto:
        @staticmethod
        def client(service, **kwargs):
            return kwargs

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage.config, "S3_ENDPOINT_URL", "https://oss-cn-guangzhou.aliyuncs.com")
    monkeypatch.setattr(object_storage.config, "S3_REGION", "")
    monkeypatch.setattr(object_storage.config, "S3_ACCESS_KEY_ID", "key")
    monkeypatch.setattr(object_storage.config, "S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(object_storage.config, "S3_BUCKET", "bucket")
    import boto3
    monkeypatch.setattr(boto3, "client", FakeBoto.client)

    client_args = object_storage._client()

    assert client_args["region_name"] == "cn-guangzhou"
    assert client_args["config"].s3["addressing_style"] == "virtual"
    assert client_args["config"].request_checksum_calculation == "when_required"
    assert client_args["config"].response_checksum_validation == "when_required"


def test_oss_upload_request_does_not_use_checksum_trailer(monkeypatch):
    import io
    import pytest

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage.config, "S3_ENDPOINT_URL", "https://oss-cn-guangzhou.aliyuncs.com")
    monkeypatch.setattr(object_storage.config, "S3_REGION", "cn-guangzhou")
    monkeypatch.setattr(object_storage.config, "S3_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(object_storage.config, "S3_SECRET_ACCESS_KEY", "test-secret")
    headers = {}

    class RequestCaptured(Exception):
        pass

    def capture(request, **kwargs):
        headers.update({key.lower(): value for key, value in request.headers.items()})
        # Inspect the signed HTTP request, stopping before any network access.
        raise RequestCaptured()

    client = object_storage._client()
    client.meta.events.register("before-send.s3.PutObject", capture)
    try:
        with pytest.raises(RequestCaptured):
            client.put_object(Bucket="test-bucket", Key="test.txt", Body=io.BytesIO(b"document"))
    finally:
        client.close()
    assert headers
    assert "x-amz-trailer" not in headers
    assert b"aws-chunked" not in headers.get("content-encoding", b"")
    assert headers.get("x-amz-content-sha256") != b"STREAMING-UNSIGNED-PAYLOAD-TRAILER"


def test_oss_signed_url_places_bucket_in_hostname(monkeypatch):
    from urllib.parse import urlparse

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage.config, "S3_ENDPOINT_URL", "https://oss-cn-guangzhou.aliyuncs.com")
    monkeypatch.setattr(object_storage.config, "S3_REGION", "cn-guangzhou")
    monkeypatch.setattr(object_storage.config, "S3_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(object_storage.config, "S3_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(object_storage.config, "S3_PUBLIC_BASE_URL", "")
    # Signing is local: this verifies the real SDK URL format without a request.
    client = object_storage._client()
    try:
        url = client.generate_presigned_url("get_object", Params={
            "Bucket": "test-bucket", "Key": "uploads/user/document.txt",
        })
    finally:
        client.close()
    assert urlparse(url).hostname == "test-bucket.oss-cn-guangzhou.aliyuncs.com"
    assert urlparse(url).path == "/uploads/user/document.txt"


def test_other_s3_endpoints_keep_path_addressing(monkeypatch):
    import boto3

    monkeypatch.setattr(object_storage, "is_configured", lambda: True)
    monkeypatch.setattr(object_storage.config, "S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: kwargs)
    assert object_storage._client()["config"].s3["addressing_style"] == "path"

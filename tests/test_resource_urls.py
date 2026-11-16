from app.services.resource_urls import asset_content_url, preview_content_url, resource_urls


def test_resource_urls_use_ids_and_drop_legacy_filename_paths():
    value = {
        "assets": [{"id": "asset-1", "fileName": "same-name.jpg", "url": "/api/v1/media/assets/same-name.jpg"}],
        "previews": [{"jobId": "job-1", "previewIndex": 0, "fileName": "same-name.jpg",
                      "url": "/api/v1/media/previews/same-name.jpg"}],
        "unrecoverable": {"url": "/api/v1/media/assets/ambiguous.jpg"},
    }
    result = resource_urls(value)
    assert result["assets"][0]["url"] == asset_content_url("asset-1")
    assert result["previews"][0]["url"] == preview_content_url("job-1", 0)
    assert result["unrecoverable"]["url"] is None
    assert "/media/" not in repr(result)


def test_historical_resource_path_is_resolved_by_exact_reference_only():
    value = {"fileName": "same-name.jpg", "objectPath": "storage://assets/owner/workspace/id-1.jpg",
             "url": "/api/v1/media/assets/same-name.jpg"}
    resolved = resource_urls(value, resolve_reference=lambda ref: "/api/v1/assets/id-1/content")
    assert resolved["url"] == "/api/v1/assets/id-1/content"
    missing = resource_urls(value, resolve_reference=lambda ref: None)
    assert missing["url"] is None

"""Resource identity URLs; filenames never act as lookup keys."""
from urllib.parse import quote


def asset_content_url(asset_id: str) -> str:
    return f"/api/v1/assets/{quote(str(asset_id), safe='')}/content"


def preview_content_url(job_id: str, preview_index: int) -> str:
    return f"/api/v1/jobs/{quote(str(job_id), safe='')}/previews/{preview_index}/content"


def resource_urls(value, *, job_id=None, resolve_reference=None):
    """Copy JSON data and replace stored URLs using IDs or exact references.

    Unknown legacy media URLs become unavailable, never filename guesses.
    """
    if isinstance(value, list):
        return [resource_urls(item, job_id=job_id, resolve_reference=resolve_reference) for item in value]
    if isinstance(value, str):
        return None if "/api/v1/media/" in value or value.startswith("/media/") else value
    if not isinstance(value, dict):
        return value
    context_job = value.get("jobId") or job_id
    result = {key: resource_urls(item, job_id=context_job, resolve_reference=resolve_reference)
              for key, item in value.items()}
    if "url" in value or ("objectPath" in value and "fileName" in value):
        index = value.get("previewIndex")
        if type(index) is int and index >= 0 and context_job:
            result["url"] = preview_content_url(context_job, index)
        elif value.get("id") and not str(value["id"]).startswith("preview-") and index is None:
            result["url"] = asset_content_url(value["id"])
        elif resolve_reference and value.get("objectPath"):
            result["url"] = resolve_reference(value["objectPath"])
        elif isinstance(value.get("url"), str) and "/media/" in value["url"]:
            result["url"] = None
    return result

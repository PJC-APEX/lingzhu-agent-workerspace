"""Rebuild historical resource URLs without changing stored JSON or guessing names."""
from app.repositories import media_repo
from app.services.resource_urls import asset_content_url, preview_content_url, resource_urls


def present_resources(db, value, user_id: str, *, job_id=None):
    references = None

    def resolve(reference):
        nonlocal references
        if references is None:
            candidates = {}
            for asset_id, ref in media_repo.owned_asset_references(db, user_id):
                candidates.setdefault(ref, set()).add(asset_content_url(asset_id))
            for preview_job_id, result in media_repo.owned_preview_results(db, user_id):
                for preview in (result or {}).get("assets", []):
                    index = preview.get("previewIndex")
                    if type(index) is int and index >= 0 and preview.get("objectPath"):
                        candidates.setdefault(preview["objectPath"], set()).add(preview_content_url(preview_job_id, index))
            references = {ref: next(iter(urls)) for ref, urls in candidates.items() if len(urls) == 1}
        return references.get(reference)

    return resource_urls(value, job_id=job_id, resolve_reference=resolve)

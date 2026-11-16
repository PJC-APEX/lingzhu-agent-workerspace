from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from app.core.config import EXPORT_DIR
from app.domain.models import new_id
from app.services.aligner import AlignmentResult
from app.services.jianying import build_timeline
from app.infrastructure import storage as object_storage


@dataclass(frozen=True)
class TimelineExport:
    file_name: str
    path: Path
    timeline: dict
    draft_url: str | None
    helper_url: str | None


def export_timeline(
    alignment: AlignmentResult,
    assets: list[dict],
    workspace_id: str,
) -> TimelineExport:
    """Build a local timeline and publish it with its images when OSS is configured."""
    file_name = f"timeline-{new_id()}.json"
    path = Path(EXPORT_DIR) / file_name
    path.parent.mkdir(parents=True, exist_ok=True)

    timeline_assets = assets
    if object_storage.is_configured():
        timeline_assets = []
        for asset in assets:
            file_name_for_asset = Path(str(asset.get("fileName") or "")).name
            if not file_name_for_asset:
                raise object_storage.ObjectStorageError("分镜图片缺少文件名")
            source_ref = str(asset.get("objectPath") or "")
            content_type = mimetypes.guess_type(file_name_for_asset)[0] or "application/octet-stream"
            if object_storage.object_key_from_reference(source_ref):
                access_url = object_storage.access_url(source_ref)
            else:
                access_url = object_storage.upload_file(
                    Path(source_ref),
                    f"assets/{workspace_id}/{file_name_for_asset}",
                    content_type,
                )
            timeline_assets.append({**asset, "objectPath": access_url})

    timeline = build_timeline(alignment, timeline_assets)
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")

    draft_url = None
    helper_link = None
    if object_storage.is_configured():
        draft_url = object_storage.upload_file(
            path,
            f"drafts/{workspace_id}/{file_name}",
            "application/json",
        )
        helper_link = object_storage.helper_url(draft_url)

    return TimelineExport(file_name, path, timeline, draft_url, helper_link)

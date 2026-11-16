from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path

from app.domain.models import AgentAction, AgentThread, Asset, Document, ExportFile, Job, SubtitleFile, Workspace
from app.infrastructure.database import SessionLocal
from app.infrastructure import storage as object_storage


def _owner_for_workspace(db, workspace_id: str) -> str:
    workspace = db.get(Workspace, workspace_id)
    if not workspace or not workspace.owner_user_id:
        raise RuntimeError(f"workspace {workspace_id} has no owner; assign legacy data ownership first")
    return workspace.owner_user_id


def _migrate_path(reference: str | None, key: str, content_type: str, dry_run: bool) -> str | None:
    if not reference or object_storage.object_key_from_reference(reference):
        return reference
    source = Path(reference)
    if not source.is_file():
        raise FileNotFoundError(f"legacy file missing: {source}")
    if dry_run:
        return f"storage://{key}"
    return object_storage.persist_file(source, key, content_type)


def _migrate_preview_list(items: list, owner_id: str, workspace_id: str, dry_run: bool) -> bool:
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        reference = item.get("objectPath")
        filename = Path(str(item.get("fileName") or reference or "preview")).name
        migrated = _migrate_path(
            reference,
            f"previews/{owner_id}/{workspace_id}/{filename}",
            mimetypes.guess_type(filename)[0] or "application/octet-stream",
            dry_run,
        )
        if migrated != reference:
            item["objectPath"] = migrated
            changed = True
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Move legacy local media files into configured S3/OSS storage.")
    parser.add_argument("--dry-run", action="store_true", help="Verify local files and list migration counts without uploading")
    args = parser.parse_args()
    if not object_storage.is_configured():
        raise SystemExit("S3/OSS is not configured. Set endpoint, bucket, access key, and secret key first.")

    counts = {"documents": 0, "assets": 0, "subtitles": 0, "exports": 0, "previews": 0}
    with SessionLocal() as db:
        for document in db.query(Document).all():
            owner = _owner_for_workspace(db, document.workspace_id)
            filename = Path(document.object_path or document.file_name).name
            document.object_path = _migrate_path(
                document.object_path,
                f"uploads/{owner}/{document.workspace_id}/{document.id}/{filename}",
                document.media_type or "application/octet-stream",
                args.dry_run,
            )
            counts["documents"] += 1

        for subtitle in db.query(SubtitleFile).all():
            owner = _owner_for_workspace(db, subtitle.workspace_id)
            directory = f"uploads/{owner}/{subtitle.workspace_id}/{subtitle.id}"
            subtitle.object_path = _migrate_path(
                subtitle.object_path,
                f"{directory}/{Path(subtitle.file_name).name}",
                "application/x-subrip",
                args.dry_run,
            )
            subtitle.audio_object_path = _migrate_path(
                subtitle.audio_object_path,
                f"{directory}/{Path(subtitle.audio_file_name or 'voiceover.bin').name}",
                mimetypes.guess_type(subtitle.audio_file_name or "")[0] or "application/octet-stream",
                args.dry_run,
            )
            counts["subtitles"] += 1

        for asset in db.query(Asset).all():
            owner = _owner_for_workspace(db, asset.workspace_id)
            asset.object_path = _migrate_path(
                asset.object_path,
                f"assets/{owner}/{asset.workspace_id}/{Path(asset.file_name).name}",
                mimetypes.guess_type(asset.file_name)[0] or "application/octet-stream",
                args.dry_run,
            )
            counts["assets"] += 1

        for export in db.query(ExportFile).all():
            job = db.get(Job, export.job_id)
            if not job:
                continue
            owner = _owner_for_workspace(db, job.workspace_id)
            export.object_path = _migrate_path(
                export.object_path,
                f"exports/{owner}/{job.workspace_id}/{Path(export.file_name).name}",
                "application/json",
                args.dry_run,
            )
            counts["exports"] += 1

        for action in db.query(AgentAction).all():
            thread = db.get(AgentThread, action.thread_id)
            workspace_id = thread.workspace_id if thread else None
            if not workspace_id:
                continue
            owner = _owner_for_workspace(db, workspace_id)
            payload = dict(action.payload or {})
            previews = payload.get("previewAssets")
            if isinstance(previews, list) and _migrate_preview_list(previews, owner, workspace_id, args.dry_run):
                payload["previewAssets"] = previews
                action.payload = payload
                counts["previews"] += len(previews)

        for job in db.query(Job).all():
            owner = _owner_for_workspace(db, job.workspace_id)
            for field in ("payload", "result"):
                value = dict(getattr(job, field) or {})
                previews = value.get("assets")
                if not isinstance(previews, list):
                    previews = value.get("previewAssets")
                if isinstance(previews, list) and _migrate_preview_list(previews, owner, job.workspace_id, args.dry_run):
                    value["assets" if "assets" in value else "previewAssets"] = previews
                    setattr(job, field, value)
                    counts["previews"] += len(previews)

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    print(json.dumps({"dryRun": args.dry_run, "migrated": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Subtitle upload orchestration; sessions are created inside the upload thread."""
from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path
import threading
from typing import BinaryIO

from app.application.workspace_service import require_workspace
from app.core import config
from app.core.container import get_session_factory
from app.domain.exceptions import TooLargeError, UnsupportedFormatError, UpstreamError, ValidationError
from app.domain.models import new_id
from app.infrastructure import file_io, storage
from app.repositories import subtitle_repo
from app.services.aligner import validate_subtitle_cues
from app.utils.files import parse_srt, safe_filename

logger = logging.getLogger(__name__)
MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
MAX_AUDIO_BYTES = 512 * 1024 * 1024
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
_UPLOAD_LIMITER = threading.BoundedSemaphore(config.UPLOAD_MAX_CONCURRENCY)


@dataclass(frozen=True)
class UploadPart:
    filename: str
    stream: BinaryIO


def acquire_upload_slot() -> bool:
    return _UPLOAD_LIMITER.acquire(blocking=False)


def release_upload_slot() -> None:
    _UPLOAD_LIMITER.release()


def subtitle_part(filename: str, stream: BinaryIO) -> UploadPart:
    filename = safe_filename(filename or "voiceover.srt")
    if Path(filename).suffix.lower() != ".srt":
        raise UnsupportedFormatError("请上传 SRT 格式字幕")
    return UploadPart(filename, stream)


def select_bundle(parts: list[UploadPart]) -> tuple[UploadPart, UploadPart]:
    subtitles = [part for part in parts if Path(part.filename).suffix.lower() == ".srt"]
    audio = [part for part in parts if Path(part.filename).suffix.lower() in AUDIO_EXTENSIONS]
    if len(subtitles) != 1 or len(audio) != 1:
        raise UnsupportedFormatError("文件夹必须恰好包含一个 SRT 和一个音频文件（MP3、WAV、M4A、AAC 或 FLAC）")
    return (
        UploadPart(safe_filename(subtitles[0].filename or "voice.srt"), subtitles[0].stream),
        UploadPart(safe_filename(audio[0].filename or "voice.mp3"), audio[0].stream),
    )


def _read_subtitle(part: UploadPart, size_message: str) -> tuple[bytes, list[dict]]:
    try:
        data = file_io.read_limited(part.stream, MAX_SUBTITLE_BYTES)
    except file_io.SizeLimitExceeded as exc:
        raise TooLargeError(size_message) from exc
    try:
        cues = parse_srt(data.decode("utf-8-sig", errors="replace"))
        validate_subtitle_cues(cues)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return data, cues


def _check_workspace(workspace_id: str, user_id: str) -> None:
    with get_session_factory()() as db:
        require_workspace(db, workspace_id, user_id)


def _save_subtitle(**values) -> dict:
    with get_session_factory()() as db:
        try:
            subtitle = subtitle_repo.add(db, **values)
            result = {"id": subtitle.id, "fileName": subtitle.file_name}
            if subtitle.audio_file_name is not None:
                result["audioFileName"] = subtitle.audio_file_name
            result["cueCount"] = len(subtitle.cues)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def _compensate(references: list[str]) -> None:
    for reference in references:
        try:
            storage.delete_reference(reference)
        except Exception:
            logger.exception("event=subtitle.cleanup_failed ref=%s", reference)


def upload_subtitle(*, workspace_id: str, user_id: str, part: UploadPart) -> dict:
    part = subtitle_part(part.filename, part.stream)
    _check_workspace(workspace_id, user_id)
    data, cues = _read_subtitle(part, "字幕文件不能超过 10MB")
    destination = config.UPLOAD_DIR / f"{new_id()}-{part.filename}"
    references = []
    try:
        file_io.write_bytes(destination, data)
        reference = storage.persist_file(
            destination, f"uploads/{user_id}/{workspace_id}/{destination.name}", "application/x-subrip",
        )
        references.append(reference)
        result = _save_subtitle(
            workspace_id=workspace_id, file_name=part.filename, object_path=reference, cues=cues,
        )
    except storage.ObjectStorageError as exc:
        _compensate(references)
        file_io.remove_file(destination)
        raise UpstreamError("subtitle storage temporarily unavailable") from exc
    except Exception:
        _compensate(references)
        file_io.remove_file(destination)
        raise
    if storage.object_key_from_reference(reference):
        file_io.remove_file(destination)
    logger.info("SRT 字幕上传完成，subtitle_id=%s, cues=%s", result["id"], len(cues))
    return result


def upload_bundle(*, workspace_id: str, user_id: str, subtitle: UploadPart, audio: UploadPart) -> dict:
    subtitle, audio = select_bundle([subtitle, audio])
    _check_workspace(workspace_id, user_id)
    data, cues = _read_subtitle(subtitle, "SRT 文件不能超过 10MB")
    directory = file_io.create_directory(config.UPLOAD_DIR / new_id())
    subtitle_path = directory / subtitle.filename
    audio_path = directory / audio.filename
    references = []
    try:
        file_io.write_bytes(subtitle_path, data)
        try:
            audio_size = file_io.write_stream(audio_path, audio.stream, MAX_AUDIO_BYTES)
        except file_io.SizeLimitExceeded as exc:
            raise TooLargeError("配音文件不能超过 512MB") from exc
        if audio_size == 0:
            raise ValidationError("配音文件为空")
        subtitle_ref = storage.persist_file(
            subtitle_path, f"uploads/{user_id}/{workspace_id}/{directory.name}/{subtitle.filename}",
            "application/x-subrip",
        )
        references.append(subtitle_ref)
        audio_ref = storage.persist_file(
            audio_path, f"uploads/{user_id}/{workspace_id}/{directory.name}/{audio.filename}",
            mimetypes.guess_type(audio.filename)[0] or "application/octet-stream",
        )
        references.append(audio_ref)
        result = _save_subtitle(
            workspace_id=workspace_id, file_name=subtitle.filename, object_path=subtitle_ref,
            audio_file_name=audio.filename, audio_object_path=audio_ref, cues=cues,
        )
    except storage.ObjectStorageError as exc:
        _compensate(references)
        file_io.remove_directory(directory)
        raise UpstreamError("subtitle or audio storage temporarily unavailable") from exc
    except Exception:
        _compensate(references)
        file_io.remove_directory(directory)
        raise
    if all(storage.object_key_from_reference(ref) for ref in references):
        file_io.remove_directory(directory)
    logger.info("配音素材包上传完成，subtitle_id=%s, cues=%s, audio_file=%s", result["id"], len(cues), audio.filename)
    return result

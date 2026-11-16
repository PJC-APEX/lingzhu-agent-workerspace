"""Adapt a prepared download to HTTP and clean staging after transmission."""
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.application.download_file import DownloadFile
from app.infrastructure import file_io


def file_response(download: DownloadFile) -> FileResponse:
    cleanup = (
        BackgroundTask(file_io.remove_directory, download.cleanup_directory)
        if download.cleanup_directory is not None else None
    )
    return FileResponse(
        download.path, filename=download.filename, media_type=download.media_type,
        headers=download.headers, background=cleanup,
    )

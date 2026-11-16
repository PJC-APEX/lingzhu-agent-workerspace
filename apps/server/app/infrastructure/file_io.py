"""Local file transport, staging and archives. No HTTP or business decisions."""
from pathlib import Path
import shutil
import tempfile
from typing import BinaryIO
import zipfile


class SizeLimitExceeded(ValueError):
    pass


def read_limited(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise SizeLimitExceeded()
    return data


def write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def write_stream(path: Path, stream: BinaryIO, limit: int) -> int:
    count = 0
    with path.open("wb") as destination:
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > limit:
                raise SizeLimitExceeded()
            destination.write(chunk)
    return count


def create_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=False)
    return path


def temporary_directory(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def remove_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def create_archive(path: Path, entries: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in entries:
            archive.write(source, name)


def is_direct_child(path: Path, directory: Path) -> bool:
    return path.resolve().parent == directory.resolve()


def exists(path: Path) -> bool:
    return path.exists()


def is_file(path: Path) -> bool:
    return path.is_file()

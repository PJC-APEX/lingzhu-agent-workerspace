"""A prepared download, independent of its HTTP transport."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DownloadFile:
    path: Path
    media_type: str
    filename: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cleanup_directory: Path | None = None

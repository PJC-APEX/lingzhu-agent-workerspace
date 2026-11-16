"""Build the downloadable Windows importer from the same draft capability."""
from pathlib import Path
from app.domain.exceptions import ServiceBusyError


def build_importer() -> Path:
    root = Path(__file__).resolve().parents[4]
    path = root / "releases" / "windows" / "LingzhuJianyingImporter.exe"
    if not path.is_file():
        raise ServiceBusyError("Windows 导入工具暂未发布，请稍后重试")
    return path

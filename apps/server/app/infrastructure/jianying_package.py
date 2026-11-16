"""Build a downloadable, self-contained media package for local Jianying import."""
import json
from pathlib import Path
import zipfile

from app.infrastructure import file_io, storage


def build_package(*, filename: str, timeline: dict, subtitle: dict, audio: dict,
                  assets: list[dict]) -> Path:
    directory = file_io.temporary_directory("lingzhu-jianying-package-")
    package_path = directory / filename
    try:
        materials = directory / "materials"
        materials.mkdir()
        for item in [subtitle, audio, *assets]:
            destination = materials / item["path"]
            storage.download_reference(item["reference"], destination)

        manifest = {
            "format": "lingzhu-jianying-package-v1",
            "timeline": timeline,
            "materials": {
                "subtitle": {"path": f"materials/{subtitle['path']}", "fileName": subtitle["fileName"]},
                "audio": {"path": f"materials/{audio['path']}", "fileName": audio["fileName"]},
                "images": [
                    {"sceneNumber": item["sceneNumber"], "path": f"materials/{item['path']}",
                     "fileName": item["fileName"]}
                    for item in assets
                ],
            },
            "importer": {"minimumVersion": 1},
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        entries = [(directory / "manifest.json", "manifest.json"),
                   (directory / "timeline.json", "timeline.json")]
        entries.extend((materials / item["path"], f"materials/{item['path']}")
                       for item in [subtitle, audio, *assets])
        file_io.create_archive(package_path, entries)
        return package_path
    except Exception:
        file_io.remove_directory(directory)
        raise

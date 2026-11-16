"""Import a Lingzhu media ZIP locally. No network access or cloud credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import zipfile

# Repository execution and the downloadable tool bundle both contain app/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "server"))
from app.services.jianying_draft import create_jianying_draft

MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 2000


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("素材包包含非法路径")
    posix = PurePosixPath(value)
    if posix.is_absolute() or PureWindowsPath(value).drive or any(p in {"..", "."} for p in value.split("/")):
        raise ValueError("素材包路径不能超出解压目录")
    if any(part.rstrip(" .") != part for part in posix.parts):
        raise ValueError("素材包路径包含不兼容 Windows 的名称")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.split(".")[0].upper() in reserved or any(c in part for c in '<>"|?*') for part in posix.parts):
        raise ValueError("素材包路径包含 Windows 保留名称")
    return posix.as_posix()


def extract_package(package: Path, directory: Path) -> dict:
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_BYTES:
            raise ValueError("素材包超过导入大小或文件数量限制")
        names = set()
        for item in entries:
            name = relative_path(item.filename.rstrip("/"))
            if name.casefold() in names or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("素材包包含重复路径或链接")
            names.add(name.casefold())
        info = archive.getinfo("manifest.json")
        if info.file_size > 4 * 1024 * 1024:
            raise ValueError("素材包清单过大")
        manifest = json.loads(archive.read(info).decode("utf-8"))
        if manifest.get("format") != "lingzhu-jianying-package-v1" or manifest.get("importer", {}).get("minimumVersion", 1) > 1:
            raise ValueError("素材包版本不受当前导入工具支持")
        # Explicit copy, never extractall or shell execution from the archive.
        for item in entries:
            target = directory / relative_path(item.filename.rstrip("/"))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        return manifest


def import_package(package: Path, drafts: Path):
    with tempfile.TemporaryDirectory(prefix="lingzhu-import-") as temp:
        root = Path(temp)
        manifest = extract_package(package, root)
        materials = manifest["materials"]

        def material(item):
            rel = relative_path(item["path"])
            if not rel.startswith("materials/") or not (root / rel).is_file():
                raise ValueError("素材文件不存在或不在 materials 目录")
            return root / rel

        images = {}
        for image in materials["images"]:
            number = image["sceneNumber"]
            if type(number) is not int or number < 1 or number in images:
                raise ValueError("图片分镜编号重复或无效")
            images[number] = material(image)
        scenes = []
        seen = set()
        previous_end = 0
        for clip in manifest["timeline"]["videoTrack"]:
            number, start, end = clip["sceneNumber"], clip["startMs"], clip["endMs"]
            if (type(number) is not int or number not in images or number in seen
                    or type(start) is not int or type(end) is not int
                    or start < previous_end or end <= start):
                raise ValueError("时间线分镜或时间范围无效")
            seen.add(number)
            previous_end = end
            scenes.append(SimpleNamespace(scene_number=number, start_ms=start, end_ms=end))
        if not scenes or seen != set(images):
            raise ValueError("时间线与图片没有一一对应")
        alignment = SimpleNamespace(scenes=scenes, subtitles=manifest["timeline"]["subtitleTrack"],
                                    match_ratio=manifest["timeline"]["alignment"]["matchRatio"])
        return create_jianying_draft(
            alignment, [{"sceneNumber": n, "objectPath": str(path)} for n, path in images.items()],
            material(materials["subtitle"]), material(materials["audio"]), drafts,
        )


def main():
    parser = argparse.ArgumentParser(description="将灵珠素材包导入本机剪映草稿")
    parser.add_argument("package", nargs="?", type=Path)
    parser.add_argument("--draft-dir", type=Path, help="自定义剪映草稿目录")
    args = parser.parse_args()
    package = args.package
    if package is None:
        from tkinter import Tk, filedialog
        window = Tk()
        window.withdraw()
        chosen = filedialog.askopenfilename(title="选择灵珠剪映素材包", filetypes=[("ZIP 素材包", "*.zip")])
        window.destroy()
        if not chosen:
            return 0
        package = Path(chosen)
    drafts = args.draft_dir or (Path.home() / "AppData/Local/JianyingPro/User Data/Projects/com.lveditor.draft")
    try:
        result = import_package(package, drafts)
    except Exception as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(f"草稿已生成：{result.path}\n请打开或重启剪映，在草稿列表中检查。")
    if os.name == "nt":
        os.startfile(str(result.path.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

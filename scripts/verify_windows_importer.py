"""Run the frozen Windows binary with isolated cwd/PATH and real media."""
from pathlib import Path
import io
import json
import os
import shutil
import subprocess
import tempfile
import wave
import zipfile

from PIL import Image

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="lingzhu-exe-test-", dir=root / ".pytest_tmp") as directory:
    staging = Path(directory)
    binary = staging / "Importer.exe"
    shutil.copyfile(root / "releases/windows/LingzhuJianyingImporter.exe", binary)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, format="PNG")
    manifest = {"format": "lingzhu-jianying-package-v1", "materials": {
        "audio": {"path": "materials/audio.wav"}, "subtitle": {"path": "materials/sub.srt"},
        "images": [{"sceneNumber": 1, "path": "materials/image.png"}]},
        "timeline": {"videoTrack": [{"sceneNumber": 1, "startMs": 0, "endMs": 1000}],
                     "subtitleTrack": [], "alignment": {"matchRatio": 1}}}
    package = staging / "input.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("materials/image.png", image.getvalue())
        archive.writestr("materials/audio.wav", audio.getvalue())
        archive.writestr("materials/sub.srt", "1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    report = staging / "report.json"
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    args = [str(binary), "--package", str(package), "--draft-dir", str(staging / "drafts"), "--report", str(report)]
    completed = subprocess.run(args, cwd=staging, env=env, timeout=120)
    result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"error": "no report"}
    assert completed.returncode == 0 and result.get("ok"), result
    draft_path = Path(result["draft"])
    draft = json.loads((draft_path / "draft_content.json").read_text(encoding="utf-8"))
    paths = [Path(item["path"]) for kind in ("videos", "audios") for item in draft["materials"][kind]]
    assert len(paths) == 2 and all(path.is_file() and path.parent == draft_path / "materials" for path in paths)
    package.unlink()
    assert all(path.is_file() for path in paths)
    print("Frozen EXE: isolated PATH/cwd, real media import, persistent media paths: passed")

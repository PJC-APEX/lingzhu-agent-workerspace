import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools/jianying_import/import_draft.py"
spec = importlib.util.spec_from_file_location("local_importer", TOOL)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def package(path, extra=None):
    manifest = {"format": "lingzhu-jianying-package-v1", "importer": {"minimumVersion": 1},
                "materials": {"audio": {"path": "materials/voice.wav"},
                              "subtitle": {"path": "materials/voice.srt"},
                              "images": [{"sceneNumber": 1, "path": "materials/image.png"}]},
                "timeline": {"videoTrack": [{"sceneNumber": 1, "startMs": 0, "endMs": 1000}],
                             "subtitleTrack": [], "alignment": {"matchRatio": 1.0}}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name in ("voice.wav", "voice.srt", "image.png"):
            archive.writestr("materials/" + name, b"material")
        if extra:
            archive.writestr(extra, b"unsafe")


@pytest.mark.parametrize("name", ["../outside.txt", "C:/outside.txt", "materials/../../outside"])
def test_importer_rejects_escaping_paths(tmp_path, name):
    source = tmp_path / "input.zip"
    package(source, name)
    target = tmp_path / "extracted"
    target.mkdir()
    with pytest.raises(ValueError):
        importer.extract_package(source, target)
    assert list(target.iterdir()) == []


def test_manifest_rejects_windows_separator():
    with pytest.raises(ValueError):
        importer.relative_path("materials\\outside")


@pytest.mark.parametrize("name", ["materials/CON.txt", "materials/LPT1", "materials/file?.jpg"])
def test_manifest_rejects_windows_device_names(name):
    with pytest.raises(ValueError):
        importer.relative_path(name)


def test_importer_passes_local_media_and_exact_timing_to_draft_builder(tmp_path, monkeypatch):
    source = tmp_path / "input.zip"
    package(source)
    drafts = tmp_path / "drafts"
    captured = {}
    def build(alignment, assets, subtitle, audio, destination):
        captured["times"] = [(scene.start_ms, scene.end_ms) for scene in alignment.scenes]
        captured["paths"] = [Path(assets[0]["objectPath"]), subtitle, audio]
        assert all(path.is_file() for path in captured["paths"])
        assert destination == drafts
        return SimpleNamespace(path=drafts / "draft")
    monkeypatch.setattr(importer, "create_jianying_draft", build)
    assert importer.import_package(source, drafts).path == drafts / "draft"
    assert captured["times"] == [(0, 1000)]
    assert all(not path.exists() for path in captured["paths"])


def test_download_serves_executable_instead_of_python_bundle(client, tmp_path, monkeypatch):
    from app.api.v1 import jianying
    binary = tmp_path / "tool.exe"
    binary.write_bytes(b"MZ-test-build")
    monkeypatch.setattr(jianying, "build_importer", lambda: binary)
    response = client.get("/api/v1/jianying/importer")
    assert response.status_code == 200
    assert response.content == b"MZ-test-build"
    assert 'LingzhuJianyingImporter.exe' in response.headers['content-disposition']
    assert binary.exists()


def test_importer_download_requires_login(client):
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/jianying/importer").status_code == 401


def test_importer_generates_real_draft_with_persistent_media(tmp_path):
    import wave
    from PIL import Image

    source = tmp_path / "real.zip"
    package(source)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(image, format="PNG")
    with zipfile.ZipFile(source) as archive:
        manifest = archive.read("manifest.json")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("materials/voice.wav", audio.getvalue())
        archive.writestr("materials/image.png", image.getvalue())
        archive.writestr("materials/voice.srt", "1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    result = importer.import_package(source, tmp_path / "drafts")
    draft = json.loads((result.path / "draft_content.json").read_text(encoding="utf-8"))
    paths = [item["path"] for kind in ("videos", "audios") for item in draft["materials"][kind]]
    assert len(paths) == 2
    assert all(Path(path).is_file() and Path(path).parent == result.path / "materials" for path in paths)
    source.unlink()
    assert all(Path(path).is_file() for path in paths)

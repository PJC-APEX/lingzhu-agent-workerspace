from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(name).name, flags=re.UNICODE)
    return cleaned[:180] or "upload.txt"


def extract_text(path: Path, file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".srt"}:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", namespace):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
            if text.strip():
                paragraphs.append(text)
        return "\n\n".join(paragraphs)
    raise ValueError("暂不支持该文件类型，请上传 TXT、MD、DOCX 或 PDF")


def parse_srt(text: str) -> list[dict]:
    import re

    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), None)
        if not time_line:
            continue
        start, end = [part.strip() for part in time_line.split("-->", 1)]
        body = " ".join(line for line in lines if line != time_line and not line.isdigit())
        if body:
            cues.append({"start": start, "end": end, "text": body})
    if not cues:
        raise ValueError("未找到有效的 SRT 字幕内容")
    return cues


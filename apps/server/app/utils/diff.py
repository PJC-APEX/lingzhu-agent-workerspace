from __future__ import annotations

from difflib import SequenceMatcher


def compare_text(original: str, rewritten: str) -> list[dict]:
    """生成可供前端标黄的差异段，保留原文和新文案的字符位置。"""
    matcher = SequenceMatcher(a=original, b=rewritten, autojunk=False)
    segments: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append({"type": "unchanged", "text": rewritten[j1:j2], "originalFrom": i1, "workingFrom": j1})
        elif tag == "delete":
            segments.append({"type": "removed", "original": original[i1:i2], "text": "", "originalFrom": i1, "workingFrom": j1})
        elif tag == "insert":
            segments.append({"type": "added", "original": "", "text": rewritten[j1:j2], "originalFrom": i1, "workingFrom": j1})
        else:
            segments.append({"type": "changed", "original": original[i1:i2], "text": rewritten[j1:j2], "originalFrom": i1, "workingFrom": j1})
    return segments


def utf16_offset_to_index(text: str, offset: int) -> int:
    """把浏览器的 UTF-16 选区偏移换成 Python 字符索引。"""
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    if units == offset:
        return len(text)
    raise ValueError("选区位置不是有效的 UTF-16 字符边界")


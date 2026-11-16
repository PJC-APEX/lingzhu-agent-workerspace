from __future__ import annotations


def create_storyboard(text: str) -> list[dict]:
    """第一版按段落生成分镜草案，后续可替换为专用模型。"""
    paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    if not paragraphs:
        paragraphs = [part.strip() for part in text.split("。") if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip() or "请补充文案"]
    return [
        {"sceneNumber": index + 1, "narration": paragraph, "imagePrompt": f"根据以下旁白生成一张画面，写实、构图清晰、无文字：{paragraph}"}
        for index, paragraph in enumerate(paragraphs[:8])
    ]


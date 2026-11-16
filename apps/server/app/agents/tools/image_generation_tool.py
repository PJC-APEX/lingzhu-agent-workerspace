from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def create_demo_image(path: Path, scene_number: int, prompt: str) -> None:
    """生成带编号的演示占位图；接入生图供应商后由真实图片替代。"""
    title = escape((prompt[:52] + "…") if len(prompt) > 52 else prompt)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="600" viewBox="0 0 960 600">
      <rect width="960" height="600" fill="#e7eeea"/><rect x="44" y="44" width="872" height="512" rx="18" fill="#d2e2db"/>
      <circle cx="740" cy="190" r="68" fill="#e7b65c"/><path d="M100 470 340 240l145 130 112-97 263 197H100Z" fill="#648d7b"/>
      <text x="80" y="110" font-family="Arial,sans-serif" font-size="26" fill="#25453a">灵珠智能助手 · 演示素材</text>
      <text x="80" y="518" font-family="Arial,sans-serif" font-size="22" fill="#25453a">分镜 {scene_number}：{title}</text>
    </svg>'''
    path.write_text(svg, encoding="utf-8")


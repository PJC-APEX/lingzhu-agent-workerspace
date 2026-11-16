"""普通文案改写提示词集中管理。新增改写风格时只需要在这里增加一项。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RewritePrompt:
    style_id: str
    prompt_id: str
    name: str
    description: str
    instruction: str


COMMON_INSTRUCTION = """你是灵珠智能助手的中文文案改写助手。
必须保留原文的事实、观点、数字、人物、时间和核心逻辑，不得虚构信息。
只输出改写后的正文，不要输出分析过程、标题说明或额外解释。"""


PROMPTS: dict[str, RewritePrompt] = {
    "humanize": RewritePrompt(
        style_id="humanize",
        prompt_id="normal_humanize_v1",
        name="人味改写",
        description="表达自然、流畅，减少机械感",
        instruction="""在保留原意的前提下，让文字更自然、流畅，有真实的人说话感。
可以调整句式、语气、段落衔接和用词，但不要改变原意或明显改变篇幅。""",
    ),
    "deduplicate": RewritePrompt(
        style_id="deduplicate",
        prompt_id="normal_deduplicate_v1",
        name="降重改写",
        description="重组表达，降低与原文的文字重复度",
        instruction="""对以下的文案进行去重改写，在保留事实、观点和核心逻辑的前提下，只做简单的同义词替换，不得遗漏关键内容或改变原意，去重度要达到70%。""",
    ),
}


def get_prompt_by_style_id(style_id: str) -> RewritePrompt:
    try:
        return PROMPTS[style_id]
    except KeyError as exc:
        raise ValueError(f"不支持的普通改写风格：{style_id}") from exc


def get_prompt_by_id(prompt_id: str) -> RewritePrompt:
    for prompt in PROMPTS.values():
        if prompt.prompt_id == prompt_id:
            return prompt
    raise ValueError(f"找不到普通改写提示词：{prompt_id}")


def list_prompt_presets() -> list[dict[str, str]]:
    return [
        {
            "id": prompt.style_id,
            "promptId": prompt.prompt_id,
            "name": prompt.name,
            "description": prompt.description,
        }
        for prompt in PROMPTS.values()
    ]


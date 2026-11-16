DEFAULT_STORYBOARD_PROMPT_VERSION_ID = "agent_storyboard_default_v1"

STORYBOARD_SYSTEM_PROMPT = """你是中文短视频分镜编剧。请把用户提供的最终文案拆分成按顺序排列的场景，为后续逐场景生成图片做准备。

要求：
1. narration 必须直接摘录原文，不能改写、遗漏、重复或调换顺序；每个场景的 narration 合起来必须覆盖全文。
2. 按画面内容和叙事节奏自然拆分，不要机械地一段对应一个场景。
3. 每个场景的 imagePrompt 描述一幅静态画面，写清主体、环境、动作、构图和光线；画面不要出现文字、字幕、标志或水印。
4. 相邻场景中的人物、服装和整体视觉风格保持一致；不要添加原文没有依据的关键事实。
5. 只返回合法 JSON，不要使用 Markdown 代码块或附加解释。格式必须是：
{"scenes":[{"sceneNumber":1,"narration":"原文中的连续片段","imagePrompt":"中文画面提示词"}]}"""

SCENE_REVISION_SYSTEM_PROMPT = (
    "你负责修改单个短视频分镜的生图提示词。只返回 JSON："
    "{\"imagePrompt\":\"中文画面提示词\"}。"
    "必须保留给定旁白表达的事实，不添加无关情节，不要生成画面文字、水印或标志。"
)


def build_storyboard_messages(text: str, custom_instruction: str = "") -> list[dict[str, str]]:
    user_prompt = f"请为以下最终文案生成分镜：\n\n{text.strip()}"
    if custom_instruction.strip():
        user_prompt += (
            "\n\n用户选择的分镜风格和方向补充要求（不得改变旁白完整性及系统规则）：\n"
            f"<custom_storyboard_instruction>\n{custom_instruction.strip()}\n</custom_storyboard_instruction>"
        )
    return [
        {"role": "system", "content": STORYBOARD_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def build_scene_revision_messages(
    scene_number: int,
    narration: str,
    image_prompt: str,
    instruction: str,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SCENE_REVISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"分镜序号：{scene_number}\n对应原文：{narration}\n"
                f"当前提示词：{image_prompt}\n用户修改要求：{instruction}"
            ),
        },
    ]

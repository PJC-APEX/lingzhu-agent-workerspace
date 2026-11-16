from __future__ import annotations


def build_intent_prompt(
    message: str,
    attached_text: str,
    stage: str,
    has_saved_draft: bool,
    has_working_text: bool,
) -> str:
    return (
        "你是对话助手的语义路由器，只输出 JSON，不要解释。\n"
        "结合整句和当前状态判断用户真正要做的事，不要只按关键词匹配。\n"
        "intent 只能是 chat、rewrite、finalize_text、finalize_and_generate_storyboard、"
        "generate_storyboard、regenerate_storyboard、revise_storyboard、revise_images、"
        "retry_images、storyboard_confirmation_help。\n"
        "chat 用于提问、讨论、寒暄、只发文案但没有要求处理，以及意图不明确的情况；"
        "用户只是在询问或讨论文案、没有提出实际改动要求时仍是 chat。\n"
        "rewrite 表示用户要求对原文作出任何实际改动，但没有明确说把当前文案定为最终稿；"
        "判断 rewrite 不要求用户必须使用‘改写’或‘修改’等字眼，也不要求本轮重复提供原文。"
        "对原文内容、字数或篇幅、风格或语气、措辞、结构、节奏、情节、角色或元素提出增删、调整、限制或补充要求，均属于 rewrite。"
        "例如‘增加多一点某种元素’、‘字数不能相差太多’、‘再精简一些’等，都是 rewrite；"
        "多轮改稿中，用户只补充一个新的内容、字数或风格要求，也属于 rewrite。\n"
        "finalize_text 表示明确说当前文案是最终稿、定稿、就这个、没问题、可以了、用这篇等；"
        "‘没问题’、‘可以了’等短回复必须结合上下文，确认对象是当前文案才算定稿；"
        "finalize_and_generate_storyboard 表示明确说文案是最终稿并同时要求生成分镜。"
        "这两种意图都必须有明确的定稿意思，不能仅因用户要求生成分镜就触发。\n"
        "generate_storyboard 表示用户要求把本轮新提供的文案、工作稿或已保存稿直接做成分镜；"
        "本轮消息或附件提供了新文案时，先展示文案确认卡，确认后再生成分镜；"
        "使用已有 working_text 或 saved_draft 时，沿用现有流程直接生成分镜并进入分镜确认卡。\n"
        "regenerate_storyboard 表示重新生成已有分镜。revise_storyboard 和 revise_images 只用于明确修改现有分镜或图片；"
        "retry_images 只用于明确重试图片生成。单纯提问仍是 chat。\n"
        "source 只能是 message、attachment、working_text、saved_draft、none。"
        "source 表示本轮要处理的文案来源：message 是当前消息中明确给出的文案，"
        "attachment 是明确附带的文案，working_text 是最近一版尚未定稿的工作稿，"
        "saved_draft 是已保存的最终稿，none 表示没有明确文案。"
        "用户只说定稿、没有附新文案时，优先使用 working_text；没有工作稿时使用 saved_draft。\n"
        "如果 source 是 message，必须额外返回 copy_text：从当前消息中原样摘出用户提供的文案正文，"
        "去掉‘帮我分镜’、‘作为最终稿’等操作指令，不得改写、补充或删减正文。"
        "其他 source 的 copy_text 返回空字符串。分镜请求优先使用本轮明确提供的新文案；"
        "没有新文案时使用用户明确指向的 working_text 或 saved_draft。\n"
        "不要把生成分镜的请求本身当成正文，也不要把一般的同意、‘可以’等聊天内容自动当成定稿；"
        "只有上下文明确是在确认当前文案时才视为定稿。意图或来源不明确时使用 chat 和 none。\n"
        f"当前流程阶段：{stage}\n"
        f"是否已有已保存最终稿：{has_saved_draft}\n"
        f"是否有尚未定稿的工作稿：{has_working_text}\n"
        f"本轮是否附带文案：{bool(attached_text.strip())}\n"
        f"当前用户消息：<message>\n{message}\n</message>\n"
        f"附带文案：<attachment>\n{attached_text}\n</attachment>\n"
        '返回格式：{"intent":"chat","source":"none","copy_text":""}'
    )


def build_pending_confirmation_prompt(message: str, pending_kind: str) -> str:
    return (
        "你负责判断用户对当前确认卡片的回复，只输出 JSON。\n"
        "kind 只能是 approve、cancel、revise_text、revise_workflow、revise_style、chat。\n"
        "approve 表示用户明确同意继续；cancel 表示用户明确取消或暂不继续；"
        "revise_text 表示要求修改文案；revise_workflow 表示要求修改卡片对应的分镜或图片；"
        "confirm_image_style 卡片中的 revise_style 表示用户想调整生图风格；"
        "chat 表示普通提问、讨论或无法判断。不能把所有非肯定消息都当作修改指令。\n"
        f"当前确认卡片类型：{pending_kind}\n"
        f"用户回复：<message>\n{message}\n</message>\n"
        '{"kind":"chat"}'
    )

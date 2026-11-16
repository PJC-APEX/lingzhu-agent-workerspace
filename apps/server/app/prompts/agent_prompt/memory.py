SUMMARY_SYSTEM_PROMPT = (
    "你负责维护中文对话的短摘要。只保留用户长期偏好、写作方向、已确认决定、事实约束和待办；"
    "删除寒暄、重复内容和长篇文案正文。不要把用户文案全文写进摘要。摘要用简短中文要点输出。"
)


def build_summary_messages(previous_summary: str, transcript: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"已有摘要：\n{previous_summary or '无'}\n\n需要并入摘要的旧对话：\n{transcript}",
        },
    ]

from __future__ import annotations

import logging

from app.services.model_gateway import ModelUnavailable, complete_chat, model_available
from app.prompts.normal_rewrite_prompts import COMMON_INSTRUCTION, get_prompt_by_id

logger = logging.getLogger(__name__)

DEMO_REPLACEMENTS = (
    ("我认为", "在我看来"),
    ("非常重要", "十分重要"),
    ("因此", "所以"),
    ("但是", "不过"),
    ("首先", "第一"),
    ("其次", "接下来"),
)


def _demo_rewrite(text: str) -> str:
    result = text
    for old, new in DEMO_REPLACEMENTS:
        result = result.replace(old, new)
    return result


def rewrite_text(text: str, prompt_id: str, user_prompt: str, model_id: str, strength: int) -> tuple[str, str]:
    """普通改写入口：按提示词编号组装固定规则和用户补充要求。"""
    rewrite_prompt = get_prompt_by_id(prompt_id)
    if not model_available(model_id):
        logger.info("模型未配置，使用本地体验改写；输入字符数=%s", len(text))
        return _demo_rewrite(text), "demo"
    try:
        result = complete_chat(
            [
                {"role": "system", "content": f"{COMMON_INSTRUCTION}\n\n当前改写策略：{rewrite_prompt.name}\n{rewrite_prompt.instruction}"},
                {"role": "user", "content": f"改写力度：{strength}/100\n用户补充要求：{user_prompt or '无，请按当前改写策略处理'}\n\n待改写文案：\n{text}"},
            ],
            model_id=model_id,
            temperature=0.65 + min(max(strength, 0), 100) / 500,
        )
        return result.text, result.mode
    except ModelUnavailable:
        logger.warning("模型调用因缺少密钥回退到体验模式")
        return _demo_rewrite(text), "demo"
    except Exception:
        logger.exception("普通文案改写调用失败")
        raise


"""Available model and rewrite preset catalog."""
from app.services.model_gateway import model_available
from app.prompts.normal_rewrite_prompts import list_prompt_presets


def get_models(capability: str = "rewrite"):
    available = any(model_available(model_id) for model_id in ("deepseek-v4-flash", "qwen3.7-plus", "doubao-2.1-pro"))
    return {
        "mode": "live" if available else "demo",
        "models": [
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash（深度求索）", "provider": "DeepSeek", "available": model_available("deepseek-v4-flash")},
            {"id": "qwen3.7-plus", "name": "Qwen3.7 Plus（通义千问）", "provider": "阿里云百炼", "available": model_available("qwen3.7-plus")},
            {"id": "doubao-2.1-pro", "name": "Doubao 2.1 Pro（豆包）", "provider": "火山方舟", "available": model_available("doubao-2.1-pro")},
        ],
        "capability": capability,
    }


def get_rewrite_presets():
    return list_prompt_presets()

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.prompts.agent_prompt.storyboard import (
    DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
    build_scene_revision_messages,
    build_storyboard_messages,
)
from app.services.model_gateway import complete_chat

logger = logging.getLogger(__name__)


class StoryboardScene(TypedDict):
    sceneNumber: int
    narration: str
    imagePrompt: str


@dataclass(frozen=True)
class StoryboardResult:
    scenes: list[StoryboardScene]
    model_id: str
    mode: str


class StoryboardOutputError(ValueError):
    """模型返回的分镜不符合约定格式或没有完整保留原文。"""


class StoryboardSceneSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sceneNumber: int = Field(default=1, ge=1)
    narration: str = Field(min_length=1)
    imagePrompt: str = Field(min_length=1)

    @field_validator("narration", "imagePrompt")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("分镜字段不能为空")
        return value


class StoryboardSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenes: list[StoryboardSceneSchema] = Field(min_length=1)


class ScenePromptRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imagePrompt: str = Field(min_length=1)

    @field_validator("imagePrompt")
    @classmethod
    def reject_blank_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("画面提示词不能为空")
        return value


def _remove_code_fence(response_text: str) -> str:
    text = response_text.strip()
    if not text.startswith("```"):
        return text

    first_line_end = text.find("\n")
    if first_line_end < 0:
        return text
    text = text[first_line_end + 1:]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3].rstrip()
    return text


def _parse_scenes(response_text: str, source_text: str) -> list[StoryboardScene]:
    try:
        response = json.loads(_remove_code_fence(response_text))
    except json.JSONDecodeError as exc:
        raise StoryboardOutputError("分镜模型没有返回有效 JSON") from exc

    try:
        parsed = StoryboardSchema.model_validate(response)
    except ValidationError as exc:
        raise StoryboardOutputError("分镜结果字段缺失或格式不正确") from exc

    scenes: list[StoryboardScene] = []
    for index, scene in enumerate(parsed.scenes, start=1):
        scenes.append({
            "sceneNumber": index,
            "narration": scene.narration.strip(),
            "imagePrompt": scene.imagePrompt.strip(),
        })

    original = "".join(source_text.split())
    storyboard_text = "".join("".join(scene["narration"].split()) for scene in scenes)
    if storyboard_text != original:
        raise StoryboardOutputError("分镜旁白没有按原文顺序完整覆盖全文，请重新生成")
    return scenes


def generate_storyboard(
    text: str,
    model_id: str,
    custom_instruction: str = "",
    prompt_id: str = DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
    prompt_version_id: str = DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
    prompt_version: int = 1,
) -> StoryboardResult:
    """调用指定模型，把最终文案拆成可供生图使用的分镜。"""
    if not text or not text.strip():
        raise ValueError("待分镜文案不能为空")
    if not model_id or not model_id.strip():
        raise ValueError("请指定分镜使用的模型")

    try:
        messages = build_storyboard_messages(text, custom_instruction)
        result = complete_chat(
            messages,
            model_id=model_id,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        # Provider JSON mode constrains syntax; local Pydantic and source checks enforce the contract.
        for attempt in range(2):
            try:
                scenes = _parse_scenes(result.text, text.strip())
                break
            except StoryboardOutputError as exc:
                logger.warning(
                    "event=storyboard.output_invalid model_id=%s attempt=%s reason=%s",
                    model_id,
                    attempt + 1,
                    str(exc),
                )
                if attempt == 1:
                    raise
                messages.extend([
                    {"role": "assistant", "content": result.text},
                    {
                        "role": "user",
                        "content": (
                            f"上次返回未通过校验，原因：{exc}。请重新生成完整 JSON。"
                            "严格满足系统定义的字段；每个 narration 必须逐字摘录原文，按顺序完整覆盖全文，"
                            "不得改写、遗漏、重复或调序。只返回 JSON。"
                        ),
                    },
                ])
                result = complete_chat(
                    messages,
                    model_id=model_id,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
    except StoryboardOutputError:
        logger.exception("event=storyboard.output_invalid_after_retry model_id=%s text_chars=%s", model_id, len(text))
        raise
    except Exception:
        logger.exception("event=storyboard.generation_failed model_id=%s text_chars=%s", model_id, len(text))
        raise

    logger.info(
        "event=storyboard.generated model_id=%s mode=%s scenes=%s text_chars=%s prompt_id=%s prompt_version_id=%s prompt_version=%s",
        model_id,
        result.mode,
        len(scenes),
        len(text),
        prompt_id,
        prompt_version_id,
        prompt_version,
    )
    return StoryboardResult(scenes=scenes, model_id=model_id, mode=result.mode)


def revise_storyboard_scene(
    scenes: list[dict],
    scene_number: int,
    instruction: str,
    model_id: str,
) -> list[StoryboardScene]:
    """只重做指定分镜的画面提示词，原旁白和其他分镜保持原样。"""
    validated = [StoryboardSceneSchema.model_validate(scene) for scene in scenes]
    selected = next((scene for scene in validated if scene.sceneNumber == scene_number), None)
    if selected is None:
        raise StoryboardOutputError(f"找不到第 {scene_number} 个分镜")
    if not instruction.strip():
        raise StoryboardOutputError("请说明要怎样调整这个分镜")

    messages = build_scene_revision_messages(scene_number, selected.narration, selected.imagePrompt, instruction)
    revision_error = None
    for attempt in range(2):
        result = complete_chat(
            messages,
            model_id=model_id,
            temperature=0.3 if attempt == 0 else 0,
            response_format={"type": "json_object"},
        )
        try:
            revised = ScenePromptRevision.model_validate(json.loads(_remove_code_fence(result.text)))
            break
        except (json.JSONDecodeError, ValidationError) as exc:
            revision_error = exc
            logger.warning(
                "event=storyboard.revision_invalid scene_number=%s model_id=%s attempt=%s",
                scene_number,
                model_id,
                attempt + 1,
            )
            if attempt == 1:
                logger.exception("event=storyboard.revision_invalid_after_retry scene_number=%s model_id=%s", scene_number, model_id)
                raise StoryboardOutputError("模型返回的分镜修改结果格式不正确，已重试一次") from exc
            messages.extend([
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": f"上次 JSON 校验失败：{exc}。只返回包含非空 imagePrompt 字段的合法 JSON。"},
            ])

    output = [scene.model_dump() for scene in validated]
    output[scene_number - 1]["imagePrompt"] = revised.imagePrompt.strip()
    logger.info("event=storyboard.scene_revised scene_number=%s model_id=%s", scene_number, model_id)
    return output

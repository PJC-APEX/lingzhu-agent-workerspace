"""Pure prompt defaults, result data and serialization; persistence lives in application/prompt_service."""
from __future__ import annotations

from dataclasses import dataclass
from app.prompts.agent_prompt.rewrite import DEFAULT_REWRITE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID
from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID

PROMPT_KINDS = {"rewrite", "storyboard", "image_style"}
DEFAULT_PROMPT_VERSIONS = {
    DEFAULT_REWRITE_PROMPT_VERSION_ID: {
        "promptId": DEFAULT_REWRITE_PROMPT_VERSION_ID,
        "versionId": DEFAULT_REWRITE_PROMPT_VERSION_ID,
        "kind": "rewrite",
        "name": "系统默认",
        "version": 1,
        "content": "",
        "isDefault": True,
    },
    DEFAULT_STORYBOARD_PROMPT_VERSION_ID: {
        "promptId": DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
        "versionId": DEFAULT_STORYBOARD_PROMPT_VERSION_ID,
        "kind": "storyboard",
        "name": "系统默认",
        "version": 1,
        "content": "",
        "isDefault": True,
    },
    DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID: {
        "promptId": DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID,
        "versionId": DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID,
        "kind": "image_style",
        "name": "系统默认",
        "version": 1,
        "content": "",
        "isDefault": True,
    },
}


@dataclass(frozen=True)
class ResolvedAgentPrompt:
    prompt_id: str
    version_id: str
    kind: str
    name: str
    version: int
    content: str
    is_default: bool


def serialize_agent_prompt_version(preset, version) -> dict:
    return {
        "promptId": preset.id,
        "versionId": version.id,
        "kind": preset.kind,
        "name": preset.name,
        "version": version.version_number,
        "content": version.content,
        "isDefault": False,
    }

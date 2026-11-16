"""Prompt resolution and version-management use cases.

Resolver helpers preserve ValueError for existing graph callers. HTTP-facing
use cases translate those errors and own commit/rollback; repositories do IO.
"""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session
from app.domain.models import AgentPromptPreset, AgentPromptVersion
from app.domain.exceptions import NotFoundError, ValidationError
from app.repositories import prompt_repo
from app.services.agent_prompt_library import (
    PROMPT_KINDS, DEFAULT_PROMPT_VERSIONS, ResolvedAgentPrompt, serialize_agent_prompt_version,
)

logger = logging.getLogger(__name__)


def list_prompts(db: Session, *, user_id: str) -> dict:
    return {kind: list_agent_prompt_presets(db, kind, user_id) for kind in sorted(PROMPT_KINDS)}


def create_prompt(db: Session, *, kind: str, name: str, content: str, user_id: str) -> dict:
    try:
        if not name.strip() or not content.strip():
            raise ValidationError("提示词名称和内容不能为空")
        preset, version = create_agent_prompt_preset(db, kind, name, content, user_id)
        db.commit()
        db.refresh(preset)
        db.refresh(version)
    except Exception:
        db.rollback()
        raise
    logger.info("event=agent.prompt_created prompt_id=%s kind=%s version=%s", preset.id, preset.kind, version.version_number)
    return serialize_agent_prompt_version(preset, version)


def create_version(db: Session, *, prompt_id: str, content: str, user_id: str) -> dict:
    try:
        if not content.strip():
            raise ValidationError("提示词内容不能为空")
        try:
            preset, version = create_agent_prompt_version(db, prompt_id, content, user_id)
        except ValueError as exc:
            raise NotFoundError(str(exc)) from exc
        db.commit()
        db.refresh(version)
    except Exception:
        db.rollback()
        raise
    logger.info("event=agent.prompt_version_created prompt_id=%s kind=%s version=%s", preset.id, preset.kind, version.version_number)
    return serialize_agent_prompt_version(preset, version)


def update_version(db: Session, *, prompt_id: str, version_id: str, content: str, user_id: str) -> dict:
    try:
        if not content.strip():
            raise ValidationError("提示词内容不能为空")
        try:
            preset, version = update_agent_prompt_version(db, prompt_id, version_id, content, user_id)
        except ValueError as exc:
            error = NotFoundError if "找不到" in str(exc) else ValidationError
            raise error(str(exc)) from exc
        db.commit()
        db.refresh(version)
    except Exception:
        db.rollback()
        raise
    logger.info("event=agent.prompt_version_updated prompt_id=%s version_id=%s", preset.id, version.id)
    return serialize_agent_prompt_version(preset, version)

def resolve_agent_prompt_version(db: Session, version_id: str, expected_kind: str, owner_user_id: str | None = None) -> ResolvedAgentPrompt:
    if expected_kind not in PROMPT_KINDS:
        raise ValueError("不支持的智能体提示词类型")
    default = DEFAULT_PROMPT_VERSIONS.get(version_id)
    if default:
        if default["kind"] != expected_kind:
            raise ValueError("提示词类型与当前操作不匹配")
        return ResolvedAgentPrompt(
            prompt_id=default["promptId"],
            version_id=default["versionId"],
            kind=default["kind"],
            name=default["name"],
            version=default["version"],
            content=default["content"],
            is_default=True,
        )

    row = prompt_repo.resolve_owned_version(db, version_id, expected_kind, owner_user_id)
    if not row:
        raise ValueError("找不到对应类型的提示词版本")
    preset, version = row
    return ResolvedAgentPrompt(
        prompt_id=preset.id,
        version_id=version.id,
        kind=preset.kind,
        name=preset.name,
        version=version.version_number,
        content=version.content,
        is_default=False,
    )


def list_agent_prompt_presets(db: Session, kind: str, owner_user_id: str | None = None) -> list[dict]:
    if kind not in PROMPT_KINDS:
        raise ValueError("不支持的智能体提示词类型")
    presets = prompt_repo.list_owned(db, kind, owner_user_id)
    result = [{
        "promptId": item["promptId"],
        "kind": item["kind"],
        "name": item["name"],
        "isDefault": True,
        "versions": [{
            "versionId": item["versionId"],
            "version": item["version"],
            "content": item["content"],
        }],
    } for item in DEFAULT_PROMPT_VERSIONS.values() if item["kind"] == kind]
    for preset in presets:
        versions = prompt_repo.list_versions(db, preset.id)
        result.append({
            "promptId": preset.id,
            "kind": preset.kind,
            "name": preset.name,
            "isDefault": False,
            "versions": [{
                "versionId": version.id,
                "version": version.version_number,
                "content": version.content,
            } for version in versions],
        })
    return result


def create_agent_prompt_preset(db: Session, kind: str, name: str, content: str, owner_user_id: str | None = None) -> tuple[AgentPromptPreset, AgentPromptVersion]:
    preset = prompt_repo.add_preset(db, kind=kind, name=name.strip(), owner_user_id=owner_user_id)
    version = prompt_repo.add_version(db, prompt_id=preset.id, version_number=1, content=content.strip())
    return preset, version


def create_agent_prompt_version(db: Session, prompt_id: str, content: str, owner_user_id: str | None = None) -> tuple[AgentPromptPreset, AgentPromptVersion]:
    preset = prompt_repo.get_owned_preset(db, prompt_id, owner_user_id)
    if not preset:
        raise ValueError("找不到自定义提示词")
    latest = prompt_repo.latest_version_number(db, prompt_id)
    version = prompt_repo.add_version(db, 
        prompt_id=preset.id,
        version_number=latest + 1,
        content=content.strip(),
    )
    return preset, version


def update_agent_prompt_version(db: Session, prompt_id: str, version_id: str, content: str, owner_user_id: str | None = None) -> tuple[AgentPromptPreset, AgentPromptVersion]:
    preset = prompt_repo.get_owned_preset(db, prompt_id, owner_user_id)
    version = prompt_repo.get_version(db, version_id)
    if not preset or not version or version.prompt_id != prompt_id:
        raise ValueError("找不到对应的自定义提示词版本")
    if preset.kind != "image_style":
        raise ValueError("只有生图风格提示词支持覆盖保存")
    version.content = content.strip()
    return preset, version

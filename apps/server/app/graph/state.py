from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoryboardSceneData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sceneNumber: int = Field(ge=1)
    narration: str = Field(min_length=1)
    imagePrompt: str = Field(min_length=1)


class GeneratedAssetData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    jobId: str = ""
    sceneNumber: int = Field(ge=1)
    fileName: str = Field(min_length=1)
    objectPath: str = Field(min_length=1)
    url: str = ""
    status: Literal["needs_review", "approved", "rejected"] = "needs_review"
    isDemo: bool = False
    previewIndex: int | None = Field(default=None, ge=0, le=1)
    prompt: str = ""


class HumanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "revise"]
    instruction: str = ""
    target: Literal["text", "workflow", "style"] = "workflow"
    selectedPreviewIndex: int | None = Field(default=None, ge=0, le=1)
    replaceStyle: bool = False


class ImageJobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobId: str = Field(min_length=1)
    assets: list[GeneratedAssetData] = Field(default_factory=list)
    error: str = ""


class TimelineClipData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sceneNumber: int = Field(ge=1)
    image: str = Field(min_length=1)
    imagePath: str = Field(min_length=1)
    start: str = Field(min_length=1)
    end: str = Field(min_length=1)
    startMs: int = Field(ge=0)
    endMs: int = Field(gt=0)
    durationMs: int = Field(gt=0)
    narration: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_duration(self):
        if self.endMs <= self.startMs or self.durationMs != self.endMs - self.startMs:
            raise ValueError("分镜图片时间长度不匹配")
        return self


class TimelineSubtitleData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    sceneNumber: int = Field(ge=1)
    text: str = Field(min_length=1)
    start: str = Field(min_length=1)
    end: str = Field(min_length=1)
    startMs: int = Field(ge=0)
    endMs: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.endMs <= self.startMs:
            raise ValueError("字幕结束时间必须晚于开始时间")
        return self


class TimelineAlignmentData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matchRatio: float = Field(ge=0, le=1)


class TimelineData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["lingzhu-timeline-v1"]
    note: str = Field(min_length=1)
    durationMs: int = Field(gt=0)
    alignment: TimelineAlignmentData
    videoTrack: list[TimelineClipData] = Field(min_length=1)
    subtitleTrack: list[TimelineSubtitleData] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tracks(self):
        scene_numbers = {clip.sceneNumber for clip in self.videoTrack}
        if any(cue.sceneNumber not in scene_numbers for cue in self.subtitleTrack):
            raise ValueError("字幕引用了不存在的分镜")
        if self.durationMs != max(cue.endMs for cue in self.subtitleTrack):
            raise ValueError("时间线总时长与字幕结束时间不一致")
        return self


class AgentGraphState(TypedDict, total=False):
    user_id: str
    thread_id: str
    workspace_id: str
    model_id: str
    message: str
    current_message_id: str
    context_text: str
    latest_assistant_text: str
    reply: str
    mode: str
    stage: str
    final_text: str
    intent: str
    rewrite_source: str
    working_text: str
    draft_text: str
    rewrite_requested: bool
    pending_revisions: list[dict]
    rewrite_prompt_version_id: str
    storyboard_prompt_version_id: str
    rewrite_prompt_id: str
    rewrite_prompt_version: int
    rewrite_used_prompt_id: str
    rewrite_used_prompt_version_id: str
    rewrite_used_prompt_version: int
    storyboard_prompt_id: str
    storyboard_prompt_version: int
    storyboard_used_prompt_id: str
    storyboard_used_prompt_version_id: str
    storyboard_used_prompt_version: int
    image_style_prompt_id: str
    image_style_prompt_version_id: str
    image_style_prompt_version: int
    image_style_prompt: str
    image_previews: list[dict]
    selected_preview: dict
    image_style_snapshot: dict
    image_style_used: dict
    storyboard: list[dict]
    image_job_id: str
    image_assets: list[dict]
    subtitle_file_id: str
    message_subtitle_file_id: str
    export_id: str
    package_job_id: str
    draft_path: str
    download_url: str
    helper_url: str | None
    pending: dict
    error: str
    resume_data: dict
    regen_scene_numbers: list[int]

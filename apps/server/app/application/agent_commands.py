"""Plain Agent inputs; field names preserve the existing request fingerprint."""
from dataclasses import asdict, dataclass

from app.prompts.agent_prompt.image_style import DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.rewrite import DEFAULT_REWRITE_PROMPT_VERSION_ID
from app.prompts.agent_prompt.storyboard import DEFAULT_STORYBOARD_PROMPT_VERSION_ID


@dataclass(frozen=True)
class MessageCommand:
    message: str
    modelId: str = "deepseek-v4-flash"
    contextText: str | None = None
    subtitleFileId: str | None = None
    rewritePromptVersionId: str = DEFAULT_REWRITE_PROMPT_VERSION_ID
    storyboardPromptVersionId: str = DEFAULT_STORYBOARD_PROMPT_VERSION_ID
    imageStylePromptVersionId: str = DEFAULT_IMAGE_STYLE_PROMPT_VERSION_ID
    retryMessageId: str | None = None
    requestId: str | None = None

    def model_dump(self, *, mode=None, exclude=()):
        """Keep fingerprint compatibility while callers migrate to plain inputs."""
        return {key: value for key, value in asdict(self).items() if key not in exclude}


@dataclass(frozen=True)
class ActionCommand:
    decision: str
    selectedPreviewIndex: int | None = None
    stylePrompt: str | None = None
    imageStylePromptVersionId: str | None = None

"""Transport-independent input for model rewrite orchestration."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RewriteCommand:
    base_revision_id: str
    scope: dict
    model_id: str
    style_id: str
    prompt: str
    strength: int
    candidate_count: int

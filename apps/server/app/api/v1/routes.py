"""Explicit API assembly; each child module owns a separate router."""
from fastapi import APIRouter

from app.api.v1 import agent, assets, auth, documents, exports, health, jobs, jianying, media, rewrite

router = APIRouter()
for child in (health, auth, agent, assets, exports, jobs, jianying, media, rewrite, documents):
    router.include_router(child.router)

__all__ = ["router"]

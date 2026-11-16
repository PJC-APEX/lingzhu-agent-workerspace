"""Public health endpoint."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health():
    return {"status": "ok", "service": "灵珠智能助手"}

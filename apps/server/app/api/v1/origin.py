"""Browser request-source policy at the HTTP boundary."""
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from app.core.config import CORS_ORIGINS

def validate_same_origin(request: Request) -> None:
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(status_code=403, detail='请求来源校验失败')
    if origin and urlsplit(origin).netloc != request.headers.get('host') and (origin.rstrip('/') not in CORS_ORIGINS):
        raise HTTPException(status_code=403, detail='请求来源校验失败')

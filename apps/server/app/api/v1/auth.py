"""HTTP authentication: validation, request origin and cookie transport."""
from __future__ import annotations
import os
import re
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from app.api.v1.deps import get_db, authenticated_user
from app.application import auth_service
from app.api.v1.origin import validate_same_origin

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
SESSION_COOKIE = "lingzhu_session"

class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=128)

    @field_validator('email')
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', value):
            raise ValueError('请输入有效邮箱')
        return value


def _set_session_cookie(response: Response, request: Request, result: auth_service.LoginResult) -> None:
    secure_setting = os.getenv("AUTH_COOKIE_SECURE", "").strip().lower()
    secure = secure_setting in {"1", "true", "yes", "on"} if secure_setting else request.url.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE, result.token, httponly=True, secure=secure,
        samesite="lax", max_age=result.max_age, path="/",
    )


@router.post("/register", status_code=201)
def register(body: AuthRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_same_origin(request)
    result = auth_service.register(db, email=body.email, password=body.password)
    _set_session_cookie(response, request, result)
    return {"user": result.user}


@router.post("/login")
def login(body: AuthRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_same_origin(request)
    result = auth_service.login(db, email=body.email, password=body.password)
    _set_session_cookie(response, request, result)
    return {"user": result.user}


@router.get("/me")
def me(user: dict = Depends(authenticated_user)):
    return {"user": user}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db), _user: dict = Depends(authenticated_user)):
    validate_same_origin(request)
    result = auth_service.logout(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return result

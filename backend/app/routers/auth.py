import hashlib
import math
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, record_audit
from app.models import User
from app.schemas import LoginRequest, LoginResponse, UserOut
from app.security import create_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 单进程滑动窗口；用户名用摘要作为键，不保存输入原文。
_login_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_LOGIN_WINDOW_SECONDS = 60
_ACCOUNT_LIMIT = 10
_SOURCE_LIMIT = 120
_MAX_BUCKETS = 4096


def _reserve_login(username: str, source: str) -> int:
    """预留尝试次数，返回需要等待的秒数；计数包括成功登录。"""
    now = time.monotonic()
    account = "account:" + hashlib.sha256(username.encode("utf-8")).hexdigest()
    source_key = "source:" + source
    with _login_lock:
        for key in list(_login_attempts):
            _login_attempts[key] = [t for t in _login_attempts[key] if now - t < _LOGIN_WINDOW_SECONDS]
            if not _login_attempts[key]:
                del _login_attempts[key]
        for key, limit in ((account, _ACCOUNT_LIMIT), (source_key, _SOURCE_LIMIT)):
            entries = _login_attempts.get(key, [])
            if len(entries) >= limit:
                return max(1, math.ceil(_LOGIN_WINDOW_SECONDS - (now - entries[0])))
        new_keys = sum(key not in _login_attempts for key in (account, source_key))
        if len(_login_attempts) + new_keys > _MAX_BUCKETS:
            return _LOGIN_WINDOW_SECONDS
        _login_attempts.setdefault(account, []).append(now)
        _login_attempts.setdefault(source_key, []).append(now)
    return 0


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    retry_after = _reserve_login(body.username, request.client.host if request.client else "unknown")
    if retry_after:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录尝试过于频繁，请稍后重试", headers={"Retry-After": str(retry_after)})
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        record_audit(db, None, "auth.login_failed", "user", user.id if user else "", {"reason": "invalid_credentials"})
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        record_audit(db, None, "auth.login_failed", "user", user.id, {"reason": "inactive"})
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")
    token, expires_at = create_session_token(user.id, user.username, user.role.value, user.password_hash)
    record_audit(db, user, "auth.login", "user", user.id)
    db.commit()
    return LoginResponse(token=token, expires_at=expires_at, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)

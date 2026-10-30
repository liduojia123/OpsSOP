import base64
import binascii
import hashlib
import hmac
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import bearer_scheme, get_current_user, record_audit
from app.models import LoginAttempt, RevokedSession, User
from app.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    ProfileUpdate,
    UserOut,
)
from app.security import create_session_token, decode_session_token, hash_password, session_token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
_LOGIN_WINDOW_SECONDS = 60
_ACCOUNT_LIMIT = 10
_SOURCE_LIMIT = 120
_MAX_AVATAR_BYTES = 256 * 1024


def _normalize_avatar(value: str) -> str:
    """只接受 PNG/JPEG/WebP 的 data URL;空串表示恢复默认头像。

    除前缀外校验解码长度与文件头,避免把任意内容当图片存储/渲染。
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    formats = {
        "data:image/png;base64,": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "data:image/jpeg;base64,": lambda data: data.startswith(b"\xff\xd8\xff"),
        "data:image/webp;base64,": lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    for prefix, check in formats.items():
        if not raw.startswith(prefix):
            continue
        try:
            decoded = base64.b64decode(raw[len(prefix):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "头像数据不是有效的 Base64") from exc
        if not decoded or len(decoded) > _MAX_AVATAR_BYTES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"头像大小必须在 1 字节到 {_MAX_AVATAR_BYTES // 1024}KB 之间",
            )
        if not check(decoded):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "头像文件头与声明的格式不符")
        return raw
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "头像仅支持 PNG、JPEG 或 WebP 图片")


def _rate_key(kind: str, value: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _reserve_login(db: Session, username: str, source: str) -> int:
    """数据库事务锁确保各进程共享限额；返回等待秒数。"""
    account, source_key = _rate_key("account", username), _rate_key("source", source)
    keys = sorted(int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True) for key in (account, source_key))
    for key in keys:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    now = db.scalar(select(func.clock_timestamp()))
    cutoff = now - timedelta(seconds=_LOGIN_WINDOW_SECONDS)
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at <= cutoff))
    for column, value, limit in ((LoginAttempt.account_key, account, _ACCOUNT_LIMIT), (LoginAttempt.source_key, source_key, _SOURCE_LIMIT)):
        count, oldest = db.execute(
            select(func.count(LoginAttempt.id), func.min(LoginAttempt.created_at)).where(column == value, LoginAttempt.created_at > cutoff)
        ).one()
        if count >= limit:
            retry = max(1, math.ceil((oldest + timedelta(seconds=_LOGIN_WINDOW_SECONDS) - now).total_seconds()))
            db.commit()
            return retry
    db.add(LoginAttempt(account_key=account, source_key=source_key, created_at=now))
    db.commit()
    return 0


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    retry_after = _reserve_login(db, body.username, request.client.host if request.client else "unknown")
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


@router.patch("/profile", response_model=UserOut)
def update_profile(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """本人可改显示名、签名与头像;不允许改角色/启用状态/密码(密码走单独接口)。"""
    changes: dict = {}
    if body.display_name is not None and body.display_name != user.display_name:
        changes["display_name"] = {"from": user.display_name, "to": body.display_name}
        user.display_name = body.display_name
    if body.signature is not None:
        signature = body.signature.strip()
        if signature != user.signature:
            changes["signature"] = "updated" if signature else "cleared"
            user.signature = signature
    if body.avatar is not None:
        avatar = _normalize_avatar(body.avatar)
        if avatar != user.avatar:
            changes["avatar"] = "updated" if avatar else "cleared"
            user.avatar = avatar
    if changes:
        record_audit(db, user, "user.update_profile", "user", user.id, changes)
        db.commit()
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    payload = decode_session_token(credentials.credentials)
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    db.execute(delete(RevokedSession).where(RevokedSession.expires_at <= datetime.now(timezone.utc)))
    db.execute(insert(RevokedSession).values(token_hash=session_token_hash(credentials.credentials), expires_at=expires_at).on_conflict_do_nothing())
    record_audit(db, user, "auth.logout", "user", user.id)
    db.commit()


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: ChangePasswordRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    retry_after = _reserve_login(db, user.username, request.client.host if request.client else "unknown")
    if retry_after:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "密码验证尝试过于频繁，请稍后重试", headers={"Retry-After": str(retry_after)})
    old_hash = user.password_hash
    if not verify_password(body.current_password, old_hash):
        record_audit(db, user, "auth.password_check_failed", "user", user.id)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "当前密码不正确")
    if verify_password(body.new_password, old_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与当前密码相同")
    result = db.execute(update(User).where(User.id == user.id, User.password_hash == old_hash).values(password_hash=hash_password(body.new_password)))
    if result.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "密码已变更，请重新登录")
    record_audit(db, user, "user.change_password", "user", user.id)
    db.commit()

"""依赖注入:当前用户、角色校验、审计写入。"""

from fastapi import Depends, HTTPException, status
from hmac import compare_digest
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditEvent, RevokedSession, Role, User
from app.security import decode_session_token, password_session_tag, session_token_hash

bearer_scheme = HTTPBearer(auto_error=True)

ROLE_RANK = {Role.operator: 0, Role.reviewer: 1, Role.admin: 2}


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_session_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录会话无效或已过期")
    if db.get(RevokedSession, session_token_hash(credentials.credentials)) is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录会话已退出")
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录会话无效或已过期")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已停用")
    tag = payload.get("password_tag")
    if not isinstance(tag, str) or not tag.isascii() or not compare_digest(tag, password_session_tag(user.password_hash)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录会话无效或已过期")
    return user


def require_role(*roles: Role):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行此操作")
        return user

    return checker


require_admin = require_role(Role.admin)
require_reviewer = require_role(Role.reviewer, Role.admin)
require_operator = require_role(Role.operator, Role.admin)


def record_audit(
    db: Session,
    actor: User | None,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            detail=detail or {},
        )
    )

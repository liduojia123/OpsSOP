"""依赖注入:当前用户、角色校验、审计写入。"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditEvent, Role, User
from app.security import decode_session_token

bearer_scheme = HTTPBearer(auto_error=True)

ROLE_RANK = {Role.operator: 0, Role.reviewer: 1, Role.admin: 2}


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_session_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录会话无效或已过期")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已停用")
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

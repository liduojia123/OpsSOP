from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, record_audit, require_admin
from app.models import User
from app.schemas import UserCreate, UserOptionOut, UserOut, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/options", response_model=list[UserOptionOut])
def user_options(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[UserOptionOut]:
    """SOP 维护人等下拉选择:任何登录用户可读的最小用户目录。"""
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.id)
    ).all()
    return [UserOptionOut.model_validate(u) for u in users]


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.id)).all()
    return [UserOut.model_validate(u) for u in users]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserOut:
    exists = db.scalar(select(User).where(User.username == body.username))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(
        username=body.username,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.flush()
    record_audit(db, admin, "user.create", "user", user.id, {"role": body.role.value})
    db.commit()
    return UserOut.model_validate(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    changes: dict = {}
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.role is not None and body.role != user.role:
        changes["role"] = {"from": user.role.value, "to": body.role.value}
        user.role = body.role
    if body.is_active is not None and body.is_active != user.is_active:
        changes["is_active"] = {"from": user.is_active, "to": body.is_active}
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    if user.id == admin.id and body.is_active is not None and not body.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己的账号")
    record_audit(db, admin, "user.update", "user", user.id, changes)
    db.commit()
    return UserOut.model_validate(user)

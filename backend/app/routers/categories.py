from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, record_audit, require_admin
from app.models import Case, Category, Fault, Sop, User
from app.schemas import CategoryCreate, CategoryOut, CategoryUpdate

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
def list_categories(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CategoryOut]:
    cats = db.scalars(select(Category).order_by(Category.sort_order, Category.id)).all()
    return [CategoryOut.model_validate(c) for c in cats]


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    body: CategoryCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> CategoryOut:
    exists = db.scalar(select(Category).where(Category.name == body.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "分类名已存在")
    cat = Category(name=body.name, description=body.description, sort_order=body.sort_order)
    db.add(cat)
    db.flush()
    record_audit(db, admin, "category.create", "category", cat.id, {"name": cat.name})
    db.commit()
    return CategoryOut.model_validate(cat)


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(
    category_id: int,
    body: CategoryUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> CategoryOut:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")
    changes: dict = {}
    if body.name is not None and body.name != cat.name:
        dup = db.scalar(select(Category).where(Category.name == body.name))
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT, "分类名已存在")
        changes["name"] = {"from": cat.name, "to": body.name}
        cat.name = body.name
    if body.description is not None:
        cat.description = body.description
    if body.sort_order is not None:
        cat.sort_order = body.sort_order
    record_audit(db, admin, "category.update", "category", cat.id, changes)
    db.commit()
    return CategoryOut.model_validate(cat)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "分类不存在")
    if cat.is_system:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "系统内置分类不可删除")
    used = (
        db.scalar(select(Fault.id).where(Fault.category_id == category_id).limit(1))
        or db.scalar(select(Case.id).where(Case.category_id == category_id).limit(1))
        or db.scalar(select(Sop.id).where(Sop.category_id == category_id).limit(1))
    )
    if used:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分类已被引用,无法删除")
    record_audit(db, admin, "category.delete", "category", cat.id, {"name": cat.name})
    db.delete(cat)
    db.commit()

"""统一检索入口:跨故障、案例、SOP 的只读搜索。

与已有 `/api/search`(知识检索,仅供 AI/引用)区分:
本接口面向页面搜索,返回可直接跳转的实体摘要,权限与各列表页一致。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import UnifiedGroupOut, UnifiedHitOut, UnifiedSearchOut
from app.search.unified import MAX_LIMIT, MAX_QUERY_CHARS, search_all

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/all", response_model=UnifiedSearchOut)
def search_entities(
    q: str = Query(default="", max_length=MAX_QUERY_CHARS),
    types: str = Query(default="", description="逗号分隔:fault,case,sop;留空表示全部"),
    limit: int = Query(default=5, ge=1, le=MAX_LIMIT),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UnifiedSearchOut:
    selected = [part.strip() for part in types.split(",") if part.strip()]
    groups = search_all(db, user, q, selected, limit)
    return UnifiedSearchOut(
        query=q.strip(),
        groups=[
            UnifiedGroupOut(
                type=group.type,
                total=group.total,
                truncated=group.truncated,
                items=[
                    UnifiedHitOut(
                        type=hit.type,
                        id=hit.id,
                        title=hit.title,
                        status=hit.status,
                        subtitle=hit.subtitle,
                        link=hit.link,
                        updated_at=hit.updated_at,
                    )
                    for hit in group.items
                ],
            )
            for group in groups
        ],
    )
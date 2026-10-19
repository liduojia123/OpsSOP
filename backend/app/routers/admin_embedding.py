"""embedding 索引管理接口(第三阶段第二批,仅管理员)。

- GET 状态:纯读取(不触发模型调用);登录/页面加载/启动不会自动构建;
- POST build/rebuild:构建或重建索引(发送知识内容至配置的 embedding 服务,
  cloud 模式经响应中的 egress_notice 明确出网范围);
- 普通运维/审核员一律 403(后端鉴权,不只隐藏按钮)。

失败原因/数量/配置标识如实返回;不伪造进度百分比,不虚构用量。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_admin
from app.models import User
from app.schemas import EmbeddingIndexBuildOut, EmbeddingIndexStatusOut
from app.search.embedding_index import (
    IndexBuildConflict,
    IndexBuildRejected,
    get_status,
    run_build,
)

router = APIRouter(prefix="/api/admin/embedding-index", tags=["admin-embedding"])


def _egress_notice(settings) -> str:
    if settings.embedding_mode == "cloud":
        return (
            "构建/重建会将脱敏后的知识内容(已通过案例与当前有效已发布 SOP 的文本)发送至"
            "已配置的云端 embedding 服务;数据库仍保存在部署所在地。"
        )
    if settings.embedding_mode == "local":
        return (
            "构建/重建会将脱敏后的知识内容发送至本地/内网 embedding 服务"
            "(不继承环境代理);不使用云端服务。"
        )
    return "embedding 未启用:不会外发任何内容。"


def _status_out(db: Session) -> EmbeddingIndexStatusOut:
    settings = get_settings()
    snap = get_status(db, settings)
    snap["egress_notice"] = _egress_notice(settings)
    return EmbeddingIndexStatusOut(**snap)


@router.get("", response_model=EmbeddingIndexStatusOut)
def index_status(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> EmbeddingIndexStatusOut:
    """索引状态查询:纯读取,不触发任何模型调用。"""
    return _status_out(db)


def _do_build(db: Session, force: bool) -> EmbeddingIndexBuildOut:
    settings = get_settings()
    try:
        outcome = run_build(db, settings, force=force)
    except IndexBuildRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except IndexBuildConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    snap = _status_out(db)
    return EmbeddingIndexBuildOut(
        result=outcome.result,
        built_units=outcome.built_units,
        reused_units=outcome.reused_units,
        embedded_calls=outcome.embedded_calls,
        usage=outcome.usage,
        skipped=outcome.skipped,
        status=snap,
    )


@router.post("/build", response_model=EmbeddingIndexBuildOut)
def build_index(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> EmbeddingIndexBuildOut:
    """构建/增量刷新索引(新代次;同指纹同内容复用向量,不重复计费)。"""
    return _do_build(db, force=False)


@router.post("/rebuild", response_model=EmbeddingIndexBuildOut)
def rebuild_index(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> EmbeddingIndexBuildOut:
    """强制重建(新代次;绕过向量复用整代重新嵌入并校验维度;失败不激活半成品,兼容旧代次保留)。"""
    return _do_build(db, force=True)

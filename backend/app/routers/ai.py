"""AI 分析与知识检索接口(第二阶段)。

- POST /api/faults/{id}/analyze:触发分析(auto/manual);分析绑定输入快照哈希、
  模型配置版本与知识版本;空输入返回 400 不调用模型;模型失败落失败记录,不回退预设答案。
- GET /api/faults/{id}/analyses:分析历史,倒序返回(最新在前)。
- GET /api/search:关键词检索正式知识(已审核案例 + 有效已发布 SOP),权限与状态在后端过滤。

权限:能查看该故障即可触发/查看分析(分析不修改故障字段;业务写权限仍限 operator/admin)。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.llm import LLMError
from app.ai.service import EmptyInput, RateLimited, revalidate_report_dict, run_analysis
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.models import AiAnalysis, Fault, FaultRecord, Role, User
from app.schemas import (
    AiAnalysisOut,
    AnalyzeRequest,
    AnalyzeResponse,
    SearchHitOut,
    SearchResponse,
)
from app.search.keyword import search_knowledge

router = APIRouter(prefix="/api", tags=["ai"])


def _get_viewable_fault(db: Session, fault_id: int, user: User) -> Fault:
    fault = db.get(Fault, fault_id)
    if fault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "故障不存在")
    if user.role == Role.operator and fault.created_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权查看该故障")
    return fault


@router.post(
    "/faults/{fault_id}/analyze",
    response_model=AnalyzeResponse,
    status_code=status.HTTP_201_CREATED,
)
def analyze_fault(
    fault_id: int,
    body: AnalyzeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AnalyzeResponse:
    """触发一次 AI 分析。不修改故障字段;模型失败不丢输入(输入仍在左栏表单中)。"""
    fault = _get_viewable_fault(db, fault_id, user)
    settings = get_settings()
    records = list(
        db.scalars(
            select(FaultRecord)
            .where(FaultRecord.fault_id == fault_id)
            .order_by(FaultRecord.id)
        ).all()
    )
    overlay = body.input.model_dump(exclude_unset=True) if body.input else None

    try:
        row, reused = run_analysis(
            db,
            user,
            fault,
            records,
            settings,
            trigger_type=body.trigger_type,
            use_cache=body.use_cache,
            input_overlay=overlay,
        )
    except EmptyInput as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except RateLimited as exc:
        # 429:前端不得立即循环请求
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    except LLMError as exc:
        # 配置错误等在调用适配器前即失败:不落库、不回退预设答案
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message)

    return AnalyzeResponse(
        analysis=AiAnalysisOut.model_validate(row),
        reused=reused,
        input_substance=True,
    )


@router.get("/faults/{fault_id}/analyses", response_model=list[AiAnalysisOut])
def list_analyses(
    fault_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AiAnalysisOut]:
    """分析历史:倒序返回(最新在前)。

    引用展示前重新读取来源状态/版本(审查 P2 #9):已失效引用从返回副本中移除
    并在 incomplete_reasons 标注;不改写历史记录本身(保留分析当时的结果)。
    """
    _get_viewable_fault(db, fault_id, user)
    rows = db.scalars(
        select(AiAnalysis)
        .where(AiAnalysis.fault_id == fault_id)
        .order_by(AiAnalysis.id.desc())
        .limit(limit)
    ).all()
    out: list[AiAnalysisOut] = []
    for r in rows:
        item = AiAnalysisOut.model_validate(r)
        if item.report:
            cleaned, stale_reasons = revalidate_report_dict(db, item.report)
            if stale_reasons:
                item = item.model_copy(
                    update={
                        "report": cleaned,
                        "incomplete_reasons": [*item.incomplete_reasons, *stale_reasons],
                    }
                )
        out.append(item)
    return out


@router.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(default="", max_length=200, description="关键词:错误码/组件/中文描述"),
    keyword_only: bool = Query(
        default=False,
        description="纯关键词路径:零 embedding 调用(AI 状态未知时客户端使用,复审 P2-4)",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SearchResponse:
    """检索正式知识(关键词 + 可用时的向量混合;向量不可用自动降级并标注原因)。

    无匹配返回空列表,不编造结果;结果片段来自当前来源,返回前再次来源复核。
    keyword_only=true 为明确的服务端纯关键词路径:不生成查询向量、零外发。
    """
    from app.search.hybrid import search_knowledge_hybrid

    result = search_knowledge_hybrid(db, user, q, get_settings(), keyword_only=keyword_only)
    return SearchResponse(
        query=q,
        hits=[
            SearchHitOut(
                ref_id=h.ref_id,
                source_type=h.source_type,
                source_id=h.source_id,
                version=h.version,
                title=h.title,
                snippet=h.snippet,
                link=h.link,
                status=h.status,
                score=h.score,
            )
            for h in result.hits
        ],
        knowledge_version=result.knowledge_version,
        retrieval_mode=result.retrieval_mode,
        degraded=result.degraded,
        degrade_reason=result.degrade_reason,
    )

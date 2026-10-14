from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import AiStatusOut

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/ai-status", response_model=AiStatusOut)
def ai_status(_: User = Depends(get_current_user)) -> AiStatusOut:
    """当前 AI 模式状态。第一阶段不调用模型,仅展示配置状态。"""
    s = get_settings()
    messages = {
        "disabled": "AI 未启用,业务功能(录入/审核/归档)不受影响。",
        "demo": "演示模式:分析结果为模拟数据,不代表真实模型能力。",
        "cloud": "云端模型模式:脱敏后的问题/日志片段将发送至配置的外部模型接口,数据库仍保存在部署所在地。",
        "local": "本地模型模式:调用本机或内网模型接口。",
    }
    return AiStatusOut(
        ai_mode=s.ai_mode,
        embedding_mode=s.embedding_mode,
        local_only=s.local_only,
        message=messages.get(s.ai_mode, "未知模式"),
    )

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ai.keys import list_user_keys, resolve_user_config
from app.ai.providers import get_provider
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
def ai_status(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiStatusOut:
    """当前 AI 模式与配置状态;查询状态不触发模型调用。"""
    s = get_settings()
    messages = {
        "disabled": "AI 未启用,业务功能(录入/审核/归档)不受影响。",
        "demo": "演示模式:分析结果为模拟数据,不代表真实模型能力。",
        "cloud": "云端模型模式:脱敏后的问题/日志片段将发送至配置的外部模型接口,数据库仍保存在部署所在地。",
        "local": "本地模型模式:调用本机或内网模型接口。",
    }
    user_config = resolve_user_config(db, user, s)
    configured = any(row.api_key_encrypted for row in list_user_keys(db, user.id))
    if user_config is not None:
        provider_name = get_provider(user_config.provider).name
        message = (
            f"已使用你自填的 {provider_name} 配置({user_config.model});"
            "问题描述与日志片段会发送到该厂商接口。"
        )
    elif configured and not s.local_only:
        message = "AI 已关闭:已保存厂商配置,可在左侧「AI 设置」中重新开启。"
    else:
        message = messages.get(s.ai_mode, "未知模式")
    return AiStatusOut(
        ai_mode=s.ai_mode,
        embedding_mode=s.embedding_mode,
        local_only=s.local_only,
        message=message,
        model_config_version=s.ai_config_version,
        user_ai_enabled=user_config is not None,
        user_provider=user_config.provider if user_config else "",
        user_ai_configured=configured,
    )

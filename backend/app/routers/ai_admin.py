"""管理员全局 AI 配置:在界面上直接启用/关闭全局模型,不再改环境变量。

- 全局配置存数据库(Key 加密),启用后对「没有自填 Key」的用户生效;
- 用户自填配置优先于全局配置;两者都没有时回落到环境变量;
- localStorage/local-only 约束与用户配置一致:禁止保存云厂商 Key;
- 任何接口不回传 Key 明文或片段。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.ai.keys import KeyError_, get_global_config, save_global_config
from app.ai.providers import ProviderError, get_provider
from app.config import get_settings
from app.database import get_db
from app.deps import record_audit, require_admin
from app.models import User
from app.schemas import AiGlobalOut, AiGlobalRequest

router = APIRouter(prefix="/api/admin/ai", tags=["ai-admin"])


def _out(row) -> AiGlobalOut:
    if row is None:
        return AiGlobalOut()
    return AiGlobalOut(
        enabled=row.enabled,
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        has_key=bool(row.api_key_encrypted),
        updated_at=row.updated_at,
    )


@router.get("", response_model=AiGlobalOut)
def read_global_config(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> AiGlobalOut:
    return _out(get_global_config(db))


@router.put("", response_model=AiGlobalOut)
def update_global_config(
    body: AiGlobalRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AiGlobalOut:
    """保存全局配置;enabled=false 即关闭全局 AI(保留已保存的 Key)。"""
    settings = get_settings()
    try:
        spec = get_provider(body.provider)
        row = save_global_config(
            db,
            admin,
            settings,
            enabled=body.enabled,
            provider=spec.id,
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model,
        )
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    except KeyError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    record_audit(
        db,
        admin,
        "ai.global.save",
        "ai",
        spec.id,
        {"enabled": row.enabled, "model": row.model, "key_updated": bool(body.api_key)},
    )
    db.commit()
    return _out(row)
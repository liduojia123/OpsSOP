"""用户自填模型厂商配置:厂商列表、Key 保存、模型列表拉取。

- 用户填写自己的 API Key(加密落库,任何接口都不回传明文或片段);
- 填好 Key 后可实时拉取该厂商的可用模型,供用户选择;
- 生效顺序:用户自填配置 > 管理员全局配置(AI_MODE/AI_BASE_URL 等);
- LOCAL_ONLY=1 时禁止配置云厂商(应用级约束,严格隔离仍需部署层限制出口);
- Key 变更写审计记录(不含 Key 内容)。
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.ai.keys import (
    KeyError_,
    get_global_config,
    delete_user_key,
    get_user_key,
    list_user_keys,
    resolve_user_config,
    save_user_key,
    set_user_ai_enabled,
)
from app.ai.providers import (
    ProviderError,
    get_provider,
    list_providers,
    normalize_base_url,
    fetch_models,
)
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, record_audit
from app.models import Role, User
from app.schemas import (
    AiKeyOut,
    AiKeyRequest,
    AiModelOut,
    AiModelsResponse,
    AiProviderOut,
    AiToggleOut,
    AiToggleRequest,
)

logger = logging.getLogger("opssop.ai.settings")

router = APIRouter(prefix="/api/ai", tags=["ai-settings"])

# 模型列表拉取限流:每用户每分钟 10 次(避免用用户 Key 反复打厂商接口)
_RATE_WINDOW_SECONDS = 60
_RATE_LIMIT = 10
_rate_lock = threading.Lock()
_rate_buckets: dict[int, list[float]] = {}


def _check_rate(user_id: int) -> None:
    now = time.monotonic()
    with _rate_lock:
        bucket = [t for t in _rate_buckets.get(user_id, []) if now - t < _RATE_WINDOW_SECONDS]
        if len(bucket) >= _RATE_LIMIT:
            _rate_buckets[user_id] = bucket
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "模型列表拉取过于频繁,请稍后再试"
            )
        bucket.append(now)
        _rate_buckets[user_id] = bucket


def _provider_out(spec, row) -> AiProviderOut:
    return AiProviderOut(
        id=spec.id,
        name=spec.name,
        base_url=spec.base_url,
        alt_base_urls=list(spec.alt_base_urls),
        docs_url=spec.docs_url,
        default_models=list(spec.default_models),
        key_prefix=spec.key_prefix,
        notes=spec.notes,
        has_key=bool(row is not None and row.api_key_encrypted),
        model=(row.model if row is not None else ""),
        user_base_url=(row.base_url if row is not None else ""),
    )


@router.get("/providers", response_model=list[AiProviderOut])
def list_available_providers(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AiProviderOut]:
    """支持的厂商清单 + 当前用户已配置状态(不含 Key)。"""
    rows = {row.provider: row for row in list_user_keys(db, user.id)}
    return [_provider_out(spec, rows.get(spec.id)) for spec in list_providers()]


@router.put("/enabled", response_model=AiToggleOut)
def toggle_ai(
    body: AiToggleRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiToggleOut:
    """用户级 AI 总开关:关闭后保留 Key 但不调用模型;开启需已有 Key。"""
    try:
        enabled, has_key = set_user_ai_enabled(db, user.id, body.enabled)
    except KeyError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    record_audit(db, user, "ai.toggle", "ai", "", {"enabled": enabled})
    db.commit()
    return AiToggleOut(enabled=enabled, has_key=has_key)


@router.get("/keys", response_model=list[AiKeyOut])
def list_my_keys(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AiKeyOut]:
    """当前用户已保存的厂商配置(只回传是否已配置,不回传 Key)。"""
    out: list[AiKeyOut] = []
    for row in list_user_keys(db, user.id):
        spec = get_provider(row.provider)
        out.append(
            AiKeyOut(
                provider=row.provider,
                name=spec.name,
                has_key=bool(row.api_key_encrypted),
                model=row.model,
                base_url=row.base_url,
                enabled=row.enabled,
                updated_at=row.updated_at,
            )
        )
    return out


@router.put("/providers/{provider}/key", response_model=AiKeyOut)
def save_key(
    provider: str,
    body: AiKeyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiKeyOut:
    """保存用户的厂商配置(Key 加密落库;api_key 留空表示只改模型/端点)。"""
    settings = get_settings()
    try:
        spec = get_provider(provider)
        row = save_user_key(
            db,
            user,
            spec.id,
            body.api_key,
            settings,
            base_url=body.base_url,
            model=body.model,
        )
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    except KeyError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    row.enabled = body.enabled
    record_audit(
        db,
        user,
        "ai.key.save",
        "ai",
        spec.id,
        {"model": row.model, "base_url": row.base_url, "key_updated": bool(body.api_key)},
    )
    db.commit()
    return AiKeyOut(
        provider=row.provider,
        name=spec.name,
        has_key=bool(row.api_key_encrypted),
        model=row.model,
        base_url=row.base_url,
        enabled=row.enabled,
        updated_at=row.updated_at,
    )


@router.delete(
    "/providers/{provider}/key",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def remove_key(
    provider: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """删除当前用户的厂商配置。"""
    spec = get_provider(provider)
    removed = delete_user_key(db, user.id, spec.id)
    if removed:
        record_audit(db, user, "ai.key.delete", "ai", spec.id, {})
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/providers/{provider}/models", response_model=AiModelsResponse)
def fetch_provider_models(
    provider: str,
    body: AiKeyRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiModelsResponse:
    """用用户自己的 Key 拉取该厂商的可用模型。

    body.api_key 可临时传入(尚未保存)用于试拉取;不传则使用已保存的 Key。
    厂商未提供列表接口时返回内置兜底清单并标注来源。
    """
    settings = get_settings()
    if settings.local_only:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "LOCAL_ONLY=1 禁止访问云端模型厂商;不允许数据外发"
        )
    _check_rate(user.id)
    spec = get_provider(provider)
    payload = body or AiKeyRequest()

    api_key = (payload.api_key or "").strip()
    base_url = payload.base_url
    if not api_key:
        row = get_user_key(db, user.id, spec.id)
        if row is None or not row.api_key_encrypted:
            # 管理员在全局配置页拉取模型时,允许使用已保存的全局 Key
            global_row = get_global_config(db) if user.role == Role.admin else None
            if global_row is not None and global_row.provider == spec.id and global_row.api_key_encrypted:
                from app.ai.keys import decrypt_secret

                try:
                    api_key = decrypt_secret(global_row.api_key_encrypted, settings.secret_key)
                except KeyError_ as exc:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
                base_url = base_url or global_row.base_url
            else:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "请先填写该厂商的 API Key"
                )
        else:
            from app.ai.keys import decrypt_secret

            try:
                api_key = decrypt_secret(row.api_key_encrypted, settings.secret_key)
            except KeyError_ as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            base_url = base_url or row.base_url

    try:
        resolved_base = normalize_base_url(spec, base_url)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    try:
        models = fetch_models(resolved_base, api_key, timeout=float(settings.ai_timeout_seconds))
    except ProviderError as exc:
        if exc.code == "auth":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
        logger.info("厂商模型列表拉取失败(%s): %s", spec.id, exc.message)
        return AiModelsResponse(
            provider=spec.id,
            models=[AiModelOut(id=model) for model in spec.default_models],
            source="fallback",
            note=f"{exc.message};以下为内置清单,可能不完整",
        )
    return AiModelsResponse(
        provider=spec.id,
        models=[
            AiModelOut(
                id=item["id"],
                name=item.get("name", ""),
                context_window=item.get("context_window"),
                max_output_tokens=item.get("max_output_tokens"),
                reasoning_levels=item.get("reasoning_levels", []),
            )
            for item in models
        ],
        source="provider",
    )


@router.get("/effective", response_model=AiProviderOut | None)
def effective_provider(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiProviderOut | None:
    """当前实际生效的厂商配置(用户自填优先,否则为全局配置)。"""
    settings = get_settings()
    config = resolve_user_config(db, user, settings)
    if config is None:
        return None
    spec = get_provider(config.provider)
    return _provider_out(spec, get_user_key(db, user.id, spec.id))

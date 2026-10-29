"""用户自填 API Key 的存储与使用。

- Key 由用户本人填写,只用于调用该用户选择的模型厂商;
- 落库前用服务端 SECRET_KEY 派生密钥加密(HMAC-SHA256 CTR + HMAC 校验),
  不新增第三方加密依赖;这是应用级保护,不是 KMS 级密钥管理;
- 任何接口都不回传 Key 明文或片段,只回传"是否已配置";
- LOCAL_ONLY=1 时禁止保存云厂商 Key(应用级约束,严格隔离仍需部署层限制出口)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.providers import (
    ProviderError,
    UserAiConfig,
    get_provider,
    normalize_base_url,
)
from app.config import Settings
from app.models import GlobalAiConfig, User, UserAiKey

_KEY_INFO = b"opssop-user-ai-key-v1"


class KeyError_(Exception):
    """密钥相关错误;message 不得包含 Key 内容。"""


def _master_key(secret_key: str) -> bytes:
    return hashlib.sha256(_KEY_INFO + secret_key.encode("utf-8")).digest()


def _keystream(master: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(master, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(secret: str, secret_key: str) -> str:
    """加密 API Key;输出 base64(nonce || mac || ciphertext)。"""
    if not secret:
        raise KeyError_("API Key 不能为空")
    master = _master_key(secret_key)
    nonce = os.urandom(16)
    raw = secret.encode("utf-8")
    ciphertext = bytes(a ^ b for a, b in zip(raw, _keystream(master, nonce, len(raw))))
    mac = hmac.new(master, nonce + ciphertext, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + mac + ciphertext).decode("ascii")


def decrypt_secret(token: str, secret_key: str) -> str:
    """解密;密文被篡改或密钥变更时拒绝(不返回任何明文片段)。"""
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyError_("API Key 密文格式无效") from exc
    if len(blob) < 33:
        raise KeyError_("API Key 密文格式无效")
    master = _master_key(secret_key)
    nonce, mac, ciphertext = blob[:16], blob[16:32], blob[32:]
    expected = hmac.new(master, nonce + ciphertext, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected):
        raise KeyError_("API Key 密文校验失败;请重新填写 API Key")
    raw = bytes(a ^ b for a, b in zip(ciphertext, _keystream(master, nonce, len(ciphertext))))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeyError_("API Key 密文无法解码") from exc


def get_user_key(db: Session, user_id: int, provider: str) -> UserAiKey | None:
    return db.scalar(
        select(UserAiKey).where(
            UserAiKey.user_id == user_id,
            UserAiKey.provider == provider,
        )
    )


def list_user_keys(db: Session, user_id: int) -> list[UserAiKey]:
    return list(
        db.scalars(
            select(UserAiKey)
            .where(UserAiKey.user_id == user_id)
            .order_by(UserAiKey.provider)
        ).all()
    )


def save_user_key(
    db: Session,
    user: User,
    provider: str,
    api_key: str,
    settings: Settings,
    *,
    base_url: str = "",
    model: str = "",
) -> UserAiKey:
    """保存或更新用户的厂商配置。

    - LOCAL_ONLY=1 时拒绝云厂商 Key(防止数据外发);
    - api_key 为空表示"保持原 Key 不变"(仅改模型或 base_url);
    - 明文 Key 不落库、不入日志、不回传前端。
    """
    spec = get_provider(provider)
    if settings.local_only:
        raise KeyError_("LOCAL_ONLY=1 禁止配置云端模型厂商的 API Key;不允许数据外发")
    resolved_base = normalize_base_url(spec, base_url)
    row = get_user_key(db, user.id, spec.id)
    if row is None:
        row = UserAiKey(user_id=user.id, provider=spec.id)
        db.add(row)
    if api_key:
        row.api_key_encrypted = encrypt_secret(api_key.strip(), settings.secret_key)
    elif not row.api_key_encrypted:
        raise KeyError_("请先填写 API Key")
    row.base_url = resolved_base
    row.model = (model or "").strip() or row.model or spec.default_models[0]
    row.enabled = True
    row.updated_at = datetime_now()
    return row


def set_user_ai_enabled(db: Session, user_id: int, enabled: bool) -> tuple[bool, bool]:
    """用户级 AI 总开关;返回 (当前启用状态, 是否已保存 Key)。

    关闭只改 enabled,不删除 Key;再次开启无需重新填写。
    未保存任何 Key 时不允许开启(否则会产生"已启用但无法调用"的假象)。
    """
    rows = list_user_keys(db, user_id)
    has_key = any(row.api_key_encrypted for row in rows)
    if enabled and not has_key:
        raise KeyError_("请先配置并保存一个厂商的 API Key,再开启 AI")
    for row in rows:
        row.enabled = enabled
    return enabled, has_key


def delete_user_key(db: Session, user_id: int, provider: str) -> bool:
    row = get_user_key(db, user_id, provider)
    if row is None:
        return False
    db.delete(row)
    return True


def resolve_user_config(
    db: Session,
    user: User,
    settings: Settings,
) -> UserAiConfig | None:
    """取该用户生效的模型配置;没有配置则返回 None(沿用管理员的全局配置)。"""
    rows = list_user_keys(db, user.id)
    if not rows:
        return None
    enabled = [row for row in rows if row.enabled and row.api_key_encrypted]
    if not enabled:
        return None
    row = enabled[0]
    try:
        spec = get_provider(row.provider)
    except ProviderError:
        return None
    if settings.local_only:
        # 云厂商在 local-only 下不可用:回落到全局配置,不外发
        return None
    try:
        api_key = decrypt_secret(row.api_key_encrypted, settings.secret_key)
    except KeyError_:
        return None
    base_url = (row.base_url or spec.base_url).rstrip("/")
    model = row.model or (spec.default_models[0] if spec.default_models else "")
    return UserAiConfig(provider=spec.id, base_url=base_url, model=model, api_key=api_key)


def get_global_config(db: Session) -> GlobalAiConfig | None:
    return db.get(GlobalAiConfig, 1)


def save_global_config(
    db: Session,
    admin: User,
    settings: Settings,
    *,
    enabled: bool,
    provider: str,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> GlobalAiConfig:
    """保存管理员全局配置;Key 加密落库,留空表示不修改。"""
    if settings.local_only:
        raise KeyError_("LOCAL_ONLY=1 禁止配置云端模型厂商的 API Key;不允许数据外发")
    spec = get_provider(provider)
    resolved_base = normalize_base_url(spec, base_url)
    row = db.get(GlobalAiConfig, 1)
    if row is None:
        row = GlobalAiConfig(id=1)
        db.add(row)
        # 单行配置:同一事务内再次保存时能查到刚插入的行(autoflush 可能关闭)
        db.flush()
    if api_key:
        row.api_key_encrypted = encrypt_secret(api_key.strip(), settings.secret_key)
    if enabled and not row.api_key_encrypted:
        raise KeyError_("请先填写全局 API Key,再开启全局 AI")
    row.enabled = enabled
    row.provider = spec.id
    row.base_url = resolved_base
    row.model = (model or "").strip() or row.model or spec.default_models[0]
    row.updated_by = admin.id
    row.updated_at = datetime_now()
    return row


def resolve_global_config(db: Session, settings: Settings) -> UserAiConfig | None:
    """管理员全局配置:启用且有 Key 时生效;local-only 下不生效。"""
    if settings.local_only:
        return None
    row = db.get(GlobalAiConfig, 1)
    if row is None or not row.enabled or not row.api_key_encrypted:
        return None
    try:
        spec = get_provider(row.provider)
        api_key = decrypt_secret(row.api_key_encrypted, settings.secret_key)
    except (ProviderError, KeyError_):
        return None
    return UserAiConfig(
        provider=spec.id,
        base_url=(row.base_url or spec.base_url).rstrip("/"),
        model=row.model or (spec.default_models[0] if spec.default_models else ""),
        api_key=api_key,
    )


def resolve_effective_ai(
    db: Session, user: User, settings: Settings
) -> UserAiConfig | None:
    """生效顺序:用户自填配置 > 管理员全局配置 > 环境变量。"""
    return resolve_user_config(db, user, settings) or resolve_global_config(db, settings)


def effective_settings(settings: Settings, config: UserAiConfig | None) -> Settings:
    """把用户配置叠加到全局设置(派生 cloud 模式与厂商端点/模型)。

    叠加后 ai_config_version 随厂商/端点/模型变化,缓存与历史不会跨模型复用。
    """
    if config is None:
        return settings
    return settings.model_copy(
        update={
            "ai_mode": "cloud",
            "ai_base_url": config.base_url,
            "ai_api_key": config.api_key,
            "ai_model": config.model,
            "ai_supports_json_mode": False,
            "ai_supports_usage": False,
        }
    )


def datetime_now():
    return datetime.now(timezone.utc)

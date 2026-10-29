"""模型厂商注册表:DeepSeek / MiniMax / 小米 MiMo。

字段全部来自各厂商官方文档(2026-10-29 核对):
- DeepSeek: https://api-docs.deepseek.com/zh-cn/
  base_url(OpenAI) https://api.deepseek.com ;模型列表 GET https://api.deepseek.com/models ;
  对话 POST https://api.deepseek.com/chat/completions ;模型 deepseek-flash、deepseek-v4-pro ;
  token 字段 max_tokens ;支持 thinking {"type":"enabled"} 与 reasoning_effort。
- MiniMax: https://platform.minimax.io/docs/
  base_url(OpenAI) https://api.minimax.io/v1(国际)/ https://api.minimaxi.com/v1(国内) ;
  模型列表 GET {base}/models ;对话 POST {base}/chat/completions ;
  模型 MiniMax-M3.1-Flash、MiniMax-M3 ;token 字段 max_completion_tokens ;
  MiniMax-M3.1-Flash 只接受 thinking {"type":"adaptive"}(disabled 会 400)。
- 小米 MiMo: https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call
  base_url(OpenAI) https://api.xiaomimimo.com/v1 ;Token Plan https://token-plan-cn.xiaomimimo.com/v1 ;
  模型列表 GET {base}/models ;对话 POST {base}/chat/completions ;
  模型 mimo-v2.6-pro、mimo-v2.6-flash、mimo-v2.6-pro-ultraspeed ;
  token 字段 max_completion_tokens ;思考内容在 reasoning_content。

三家都是 OpenAI 兼容的 chat/completions,差异集中在 token 字段名与个别模型的
必填参数,这里显式建模,不假设行为一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("opssop.ai.providers")


class ProviderError(Exception):
    """厂商接口错误;message 不得包含 API Key。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str
    docs_url: str = ""
    alt_base_urls: tuple[str, ...] = ()
    default_models: tuple[str, ...] = ()
    token_field: str = "max_tokens"
    # 需要强制带 thinking 参数的模型(小写前缀匹配);MiniMax-M3.1-Flash 只接受 adaptive
    required_thinking_models: tuple[str, ...] = ()
    thinking_type: str = "adaptive"
    key_prefix: str = ""
    notes: str = ""

    def models_url(self, base_url: str | None = None) -> str:
        return (base_url or self.base_url).rstrip("/") + "/models"

    def extra_body(self, model: str) -> dict[str, Any]:
        """按模型补齐厂商要求的额外参数(未在文档中确认的一律不发)。"""
        lowered = (model or "").lower()
        for prefix in self.required_thinking_models:
            if lowered.startswith(prefix.lower()):
                return {"thinking": {"type": self.thinking_type}}
        return {}


PROVIDERS: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in [
        ProviderSpec(
            id="deepseek",
            name="DeepSeek",
            base_url="https://api.deepseek.com",
            docs_url="https://api-docs.deepseek.com/zh-cn/",
            default_models=("deepseek-flash", "deepseek-v4-pro"),
            token_field="max_tokens",
            key_prefix="sk-",
            notes="OpenAI 兼容;模型列表 GET /models;深度思考需 thinking 参数。",
        ),
        ProviderSpec(
            id="minimax",
            name="MiniMax",
            base_url="https://api.minimaxi.com/v1",
            alt_base_urls=("https://api.minimax.io/v1",),
            docs_url="https://platform.minimax.io/docs/api-reference/api-overview",
            default_models=("MiniMax-M3.1-Flash", "MiniMax-M3"),
            token_field="max_completion_tokens",
            required_thinking_models=("MiniMax-M3.1-Flash",),
            key_prefix="sk-",
            notes="国内 api.minimaxi.com/v1,国际 api.minimax.io/v1;M3.1-Flash 必须带 thinking=adaptive。",
        ),
        ProviderSpec(
            id="mimo",
            name="小米 MiMo",
            base_url="https://api.xiaomimimo.com/v1",
            alt_base_urls=("https://token-plan-cn.xiaomimimo.com/v1",),
            docs_url="https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call",
            default_models=("mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"),
            token_field="max_completion_tokens",
            key_prefix="sk-",
            notes="OpenAI 兼容;思考内容在 reasoning_content;V2.5 系列已下线。",
        ),
    ]
}


def list_providers() -> list[ProviderSpec]:
    return list(PROVIDERS.values())


def get_provider(provider_id: str) -> ProviderSpec:
    spec = PROVIDERS.get((provider_id or "").strip().lower())
    if spec is None:
        raise ProviderError("unknown_provider", f"未知的模型厂商: {provider_id}")
    return spec


def normalize_base_url(spec: ProviderSpec, base_url: str | None) -> str:
    """用户可覆盖 base_url;为空则用厂商默认;必须是 https 的绝对地址。"""
    value = (base_url or "").strip() or spec.base_url
    if not value.startswith("https://"):
        raise ProviderError("bad_base_url", "base_url 必须以 https:// 开头")
    if len(value) > 300:
        raise ProviderError("bad_base_url", "base_url 过长")
    return value.rstrip("/")


def parse_models_payload(payload: Any) -> list[dict]:
    """解析 OpenAI 兼容的模型列表;DeepSeek 额外返回上下文/输出上限与推理档位。"""
    if not isinstance(payload, dict):
        raise ProviderError("bad_response", "模型列表响应不是 JSON 对象")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ProviderError("bad_response", "模型列表响应缺少 data 数组")
    models: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        entry: dict[str, Any] = {"id": model_id.strip()}
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()
        for key in ("context_window", "max_output_tokens"):
            value = item.get(key)
            if isinstance(value, int):
                entry[key] = value
        effort = item.get("effort")
        if isinstance(effort, dict):
            levels = effort.get("supported_levels")
            if isinstance(levels, list):
                entry["reasoning_levels"] = [str(x) for x in levels]
        models.append(entry)
    if not models:
        raise ProviderError("bad_response", "模型列表为空")
    models.sort(key=lambda m: m["id"])
    return models


def fetch_models(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 20.0,
    trust_env: bool = True,
) -> list[dict]:
    """调用厂商模型列表接口;失败抛出 ProviderError(不含 Key)。"""
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=timeout, trust_env=trust_env) as client:
            resp = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", "模型列表接口超时", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise ProviderError("connect", "模型列表接口连接失败", retryable=True) from exc
    if resp.status_code in (401, 403):
        raise ProviderError("auth", "模型列表接口鉴权失败;请检查 API Key")
    if resp.status_code == 404:
        raise ProviderError("not_found", "该厂商未提供模型列表接口")
    if resp.status_code >= 500:
        raise ProviderError("upstream", f"模型列表接口错误(HTTP {resp.status_code})", retryable=True)
    if resp.status_code >= 400:
        raise ProviderError("bad_request", f"模型列表接口被拒绝(HTTP {resp.status_code})")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ProviderError("bad_response", "模型列表响应不是合法 JSON") from exc
    return parse_models_payload(payload)


@dataclass
class ProviderModels:
    """模型列表结果:来源标记区分"厂商实时返回"与"内置兜底"。"""

    provider: str
    models: list[dict] = field(default_factory=list)
    source: str = "provider"  # provider | fallback
    note: str = ""


@dataclass(frozen=True)
class UserAiConfig:
    """用户生效的模型配置(纯数据;Key 明文只在服务端内存中短暂存在)。"""

    provider: str
    base_url: str
    model: str
    api_key: str

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key),
        }

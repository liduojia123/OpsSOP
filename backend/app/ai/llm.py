"""聊天模型适配层:优先 OpenAI 兼容接口,能力显式配置,失败不回退预设答案。

模式(环境变量 AI_MODE):
- disabled: 不调用模型;人工业务与关键词检索不受影响;
- demo: 仅显式开启(AI_MODE=demo)时使用本地模拟分析,每条结果标注演示数据;
- cloud / local: 调用配置的 OpenAI 兼容接口;失败即失败,不回退 demo 或云端。

能力配置(AI_SUPPORTS_JSON_MODE / AI_SUPPORTS_USAGE):
不同服务对 response_format、usage 字段支持不一致,默认保守(关闭),
由管理员按目标服务显式开启;不假设所有服务行为一致。

LOCAL_ONLY=1 时:
- 禁止 cloud 模式(启动校验 + 调用前复检,防止绕过);
- local 端点必须是管理员配置的受信任本机/内网地址(默认拒绝公网主机名)。
应用级配置约束 != 网络出口隔离,严格隔离需部署环境限制出口。

代理:local 模式构造客户端时 trust_env=False,不继承 HTTP_PROXY/HTTPS_PROXY/
ALL_PROXY/NO_PROXY,防止部署环境的外部代理接收内网模型请求(应用数据经外部目标中转);
cloud 模式保留部署环境显式配置的代理(属基础设施)。代理绕过是审查 P1 #4 场景。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.ai.providers import UserAiConfig, get_provider
from app.ai.sanitize import sanitize_text
from app.config import Settings

logger = logging.getLogger("opssop.ai")

# 输出侧日志不打印请求头/密钥;错误信息经 redact 后返回前端


class LLMError(Exception):
    """模型调用失败的统一异常;message 不得包含密钥。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code  # timeout | rate_limited | connect | auth | bad_response | disabled | config
        self.message = message
        self.retryable = retryable


@dataclass
class LLMResult:
    text: str
    usage: dict[str, int] | None  # 仅服务端实际返回 usage 时填充,否则 None
    model: str
    latency_ms: int
    # 模型请求调用工具时的原始 tool_calls(OpenAI 兼容结构);普通文本响应为空
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class LLMCapabilities:
    """模型服务能力:显式配置,不猜测。"""

    supports_json_mode: bool = False
    supports_usage: bool = False  # False 时即使响应带 usage 也只按「未知」记录?不:带了就记;此标记仅提示
    max_context_chars: int = 24000
    max_output_tokens: int = 2048


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    # assistant 消息回填的工具调用;tool 消息对应的调用 ID
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMRequest:
    messages: list[ChatMessage]
    max_output_tokens: int = 2048
    temperature: float = 0.2
    # 仅结构化输出请求开启;capability 只表示服务支持与否,不强制所有请求使用
    # (DeepSeek 等服务在提示词不含 "json" 时会拒绝 json_object 并返回 400)
    json_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    # OpenAI 兼容 function calling;为空表示不使用工具
    tools: list[dict] | None = None
    tool_choice: str | None = None


class ChatAdapter(Protocol):
    def chat(self, request: LLMRequest) -> LLMResult: ...

    def describe(self) -> dict: ...


def _message_payload(message: ChatMessage) -> dict:
    """OpenAI 兼容消息体;tool 消息带 tool_call_id,assistant 消息带回 tool_calls。"""
    payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id or ""
        if message.name:
            payload["name"] = message.name
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = message.tool_calls
    return payload


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[已截断]"


class DisabledAdapter:
    """AI 关闭:明确报错,不产生任何模拟结果。"""

    mode = "disabled"

    def chat(self, request: LLMRequest) -> LLMResult:
        raise LLMError("disabled", "AI 未启用(AI_MODE=disabled);可继续人工处理与关键词检索")

    def describe(self) -> dict:
        return {"mode": "disabled"}


class DemoAdapter:
    """演示模式:确定性模拟分析;结果强制标注演示数据,不代表真实模型能力。

    仅在 AI_MODE=demo(显式开启)时使用;真实接口失败不会走到这里。
    """

    mode = "demo"

    def chat(self, request: LLMRequest) -> LLMResult:
        # demo 的模拟报告由 service 层根据输入生成;这里只提供占位文本通道
        payload = {
            "demo": True,
            "note": "演示数据 / 模拟分析",
            "echo": _truncate(request.messages[-1].content if request.messages else "", 200),
        }
        return LLMResult(
            text=json.dumps(payload, ensure_ascii=False),
            usage=None,  # 模拟响应不虚构 usage
            model="demo-simulated",
            latency_ms=0,
        )

    def describe(self) -> dict:
        return {"mode": "demo"}


@dataclass
class OpenAICompatEndpoint:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    # 厂商差异:MiniMax/MiMo 使用 max_completion_tokens,DeepSeek 使用 max_tokens
    token_field: str = "max_tokens"
    # 厂商要求的额外参数(如 MiniMax-M3.1-Flash 必须带 thinking=adaptive)
    extra_body: dict[str, Any] = field(default_factory=dict)


class OpenAICompatAdapter:
    """OpenAI 兼容 chat/completions 适配器(cloud/local 共用)。

    - 密钥只放在请求头,不写入日志、异常信息与分析记录;
    - 429 记为 rate_limited(retryable),由上层决定退避,不立即循环重试;
    - 超时/连接失败/非 2xx/非法 JSON 均抛 LLMError,不回退预设答案。
    """

    mode = "remote"

    def __init__(
        self,
        endpoint: OpenAICompatEndpoint,
        capabilities: LLMCapabilities,
        trust_env: bool = True,
    ):
        self.endpoint = endpoint
        self.capabilities = capabilities
        # trust_env=False:不继承 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY
        # (local 模式使用,防止应用数据经外部代理出网,见 build_adapter)
        self.trust_env = trust_env

    def chat(self, request: LLMRequest) -> LLMResult:
        url = self.endpoint.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": [_message_payload(m) for m in request.messages],
            self.endpoint.token_field: min(request.max_output_tokens, self.capabilities.max_output_tokens),
            "temperature": request.temperature,
        }
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = request.tool_choice or "auto"
        # 厂商必填参数(如 thinking)放在最后,避免覆盖上面的字段
        body.update(self.endpoint.extra_body or {})
        if request.json_mode and self.capabilities.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.endpoint.api_key:
            headers["Authorization"] = f"Bearer {self.endpoint.api_key}"

        import time

        start = time.monotonic()
        try:
            with httpx.Client(
                timeout=self.endpoint.timeout_seconds, trust_env=self.trust_env
            ) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(
                "timeout", f"模型调用超时(>{self.endpoint.timeout_seconds}s)", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            # 异常串可能含 URL,统一脱敏后再用
            raise LLMError("connect", f"模型连接失败: {sanitize_text(str(exc))}", retryable=True) from exc

        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 429:
            raise LLMError("rate_limited", "模型服务限流(HTTP 429);请稍后再试", retryable=True)
        if resp.status_code in (401, 403):
            raise LLMError("auth", "模型服务鉴权失败;请检查 API Key 配置", retryable=False)
        if resp.status_code >= 500:
            raise LLMError("upstream", f"模型服务错误(HTTP {resp.status_code})", retryable=True)
        if resp.status_code >= 400:
            raise LLMError(
                "bad_request",
                f"模型请求被拒绝(HTTP {resp.status_code});可能是参数能力不匹配,"
                "可调整 AI_SUPPORTS_JSON_MODE 等能力配置",
                retryable=False,
            )

        try:
            data = resp.json()
            message = data["choices"][0]["message"]
            text = message.get("content") or ""
            raw_calls = message.get("tool_calls") or []
            tool_calls = [c for c in raw_calls if isinstance(c, dict)] if isinstance(raw_calls, list) else []
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("bad_response", "模型响应不是预期的 chat/completions 结构") from exc
        if not isinstance(text, str):
            text = ""
        if not text.strip() and not tool_calls:
            raise LLMError("bad_response", "模型返回内容为空")

        usage: dict[str, int] | None = None
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                k: int(v)
                for k, v in raw_usage.items()
                if isinstance(v, (int, float)) and k in ("prompt_tokens", "completion_tokens", "total_tokens")
            } or None

        return LLMResult(
            text=text,
            usage=usage,
            model=str(data.get("model", self.endpoint.model)),
            latency_ms=latency_ms,
            tool_calls=tool_calls,
        )

    def describe(self) -> dict:
        # 只暴露非敏感配置;api_key 只暴露是否已配置
        return {
            "mode": self.mode,
            "base_url": self.endpoint.base_url,
            "model": self.endpoint.model,
            "has_api_key": bool(self.endpoint.api_key),
            "timeout_seconds": self.endpoint.timeout_seconds,
            "supports_json_mode": self.capabilities.supports_json_mode,
            "supports_usage": self.capabilities.supports_usage,
            "trust_env_proxy": self.trust_env,
            "token_field": self.endpoint.token_field,
        }


_TRUSTED_LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "host.docker.internal",
    "host.containers.internal",
}


def _is_trusted_local_target(base_url: str) -> bool:
    """local 模式目标须是管理员配置的受信任本机/内网地址。

    只做应用级校验:拒绝明显公网主机名;内网 IP/自定义主机名由管理员负责配置正确。
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _TRUSTED_LOCAL_HOSTS:
        return True
    # IPv4 内网/环回
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or a == 127:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        return False
    # 明显公网域名拒绝;内网自定义域名(含 .local/.internal/.lan/.home)允许
    if host.endswith((".local", ".internal", ".lan", ".home", ".intranet")):
        return True
    return False


def build_adapter(settings: Settings, user_config: UserAiConfig | None = None) -> ChatAdapter:
    """按配置构造适配器。构造即校验 LOCAL_ONLY 与 local 目标可信。

    user_config:用户自填厂商配置(优先于管理员全局配置);此时按厂商文档使用
    对应的 token 字段与必填参数,并在 LOCAL_ONLY 下拒绝外发。
    """
    if user_config is not None:
        spec = get_provider(user_config.provider)
        if settings.local_only:
            raise LLMError("config", "LOCAL_ONLY=1 禁止调用云端模型;不允许数据外发")
        if not user_config.api_key:
            raise LLMError("config", "用户模型配置缺少 API Key")
        if not user_config.model:
            raise LLMError("config", "用户模型配置缺少模型名")
        return OpenAICompatAdapter(
            OpenAICompatEndpoint(
                base_url=user_config.base_url,
                model=user_config.model,
                api_key=user_config.api_key,
                timeout_seconds=float(settings.ai_timeout_seconds),
                token_field=spec.token_field,
                extra_body=spec.extra_body(user_config.model),
            ),
            LLMCapabilities(
                # 厂商的结构化输出/usage 行为不一致,用户配置默认保守关闭
                supports_json_mode=False,
                supports_usage=False,
                max_context_chars=settings.ai_max_context_chars,
                max_output_tokens=settings.ai_max_output_tokens,
            ),
            trust_env=True,
        )

    mode = settings.ai_mode
    if mode == "disabled":
        return DisabledAdapter()
    if mode == "demo":
        return DemoAdapter()

    capabilities = LLMCapabilities(
        supports_json_mode=settings.ai_supports_json_mode,
        supports_usage=settings.ai_supports_usage,
        max_context_chars=settings.ai_max_context_chars,
        max_output_tokens=settings.ai_max_output_tokens,
    )

    if mode == "cloud":
        if settings.local_only:
            # 双保险:启动校验之外,新调用前复检,禁止绕过
            raise LLMError("config", "LOCAL_ONLY=1 禁止 cloud 模型调用;不允许数据外发")
        if not settings.ai_base_url:
            raise LLMError("config", "AI_MODE=cloud 但未配置 AI_BASE_URL")
    elif mode == "local":
        if not settings.ai_base_url:
            raise LLMError("config", "AI_MODE=local 但未配置 AI_BASE_URL")
        if not _is_trusted_local_target(settings.ai_base_url):
            raise LLMError(
                "config",
                "AI_MODE=local 的目标地址不是受信任的本机/内网地址;"
                "请由管理员配置正确的内网端点(应用级校验不等于网络隔离)",
            )
    else:
        raise LLMError("config", f"未知 AI_MODE: {mode}")

    return OpenAICompatAdapter(
        OpenAICompatEndpoint(
            base_url=settings.ai_base_url,
            model=settings.ai_model,
            api_key=settings.ai_api_key,
            timeout_seconds=float(settings.ai_timeout_seconds),
        ),
        capabilities,
        # local 模式直连配置的内网端点,不继承环境代理(防止经外部代理中转出网);
        # 仅校验 URL 目标主机不够,代理会先于目标收到请求正文。
        trust_env=(mode != "local"),
    )

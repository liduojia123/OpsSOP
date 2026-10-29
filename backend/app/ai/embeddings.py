"""embedding 适配层:与聊天模型独立配置,优先 OpenAI 兼容接口。

模式(环境变量 EMBEDDING_MODE):
- disabled: 不调用 embedding;基本业务与关键词检索不受影响;不静默生成伪向量;
- cloud: 调用配置的云 embedding 接口;
- local: 调用本机/内网 embedding 接口(受信任目标校验,不继承环境代理)。

适配边界(不假设各服务行为一致):
- 请求只带 model/input 等最小参数;能力差异由服务端错误暴露,不自动换服务;
- 严格校验返回:条数一致、索引对应关系(0..n-1)、维度一致、非空向量、数值有限;
- 超时/批量上限/输入长度/有限重试由调用方与本层共同约束;
- 失败不自动切换另一服务或切换为云端;usage 缺失保持未知,不虚构 token/费用/成功;
- LOCAL_ONLY 同时约束聊天与 embedding:cloud 配置冲突明确拒绝;
- 发送内容在最终请求边界统一脱敏;密钥不进日志/错误信息/前端。

合成向量只存在于测试夹具,不进入生产路径;disabled 下不产生任何向量。
"""

import logging
import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.ai.llm import _is_trusted_local_target
from app.ai.sanitize import sanitize_text
from app.config import Settings

logger = logging.getLogger("opssop.embedding")

# 瞬时错误(超时/连接/5xx)在有限重试前的最小退避;退避后仍失败即由调用方降级/失败。
# 429 限流不在此列:上游已要求放慢时,同步立即重发只会增加一次相同请求,
# 因此不重试、不退避,直接降级(检索)或失败(索引)。
RETRY_BACKOFF_SECONDS = 0.5


class EmbeddingError(Exception):
    """embedding 调用/配置失败的统一异常;message 不得包含密钥。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code  # disabled | config | timeout | rate_limited | connect | auth | upstream | bad_request | bad_response
        self.message = message
        self.retryable = retryable


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    usage: dict | None  # 仅服务端实际返回时填充;缺失为 None(未知)
    model: str
    dims: int


class EmbeddingAdapter(Protocol):
    def embed(self, texts: list[str]) -> EmbeddingResult: ...

    def describe(self) -> dict: ...


class DisabledEmbeddingAdapter:
    """embedding 关闭:明确报错,不产生任何伪向量。"""

    mode = "disabled"

    def embed(self, texts: list[str]) -> EmbeddingResult:
        raise EmbeddingError(
            "disabled", "embedding 未启用(EMBEDDING_MODE=disabled);可使用关键词检索"
        )

    def describe(self) -> dict:
        return {"mode": "disabled"}


def _validate_embeddings(payload_texts: list[str], data: list) -> list[list[float]]:
    """校验返回条数、索引对应、维度一致、非空向量、数值有限;异常一律拒绝。"""
    if not isinstance(data, list):
        raise EmbeddingError("bad_response", "embedding 响应缺少 data 列表")
    if len(data) != len(payload_texts):
        raise EmbeddingError(
            "bad_response",
            f"embedding 返回条数与输入不一致(输入 {len(payload_texts)},返回 {len(data)})",
        )
    vectors: list[list[float]] = [None] * len(payload_texts)  # type: ignore[list-item]
    dims: int | None = None
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingError("bad_response", "embedding 返回项不是对象")
        idx = item.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(payload_texts):
            raise EmbeddingError("bad_response", "embedding 返回索引无效或缺失")
        if vectors[idx] is not None:
            raise EmbeddingError("bad_response", f"embedding 返回索引重复({idx})")
        vec = item.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbeddingError("bad_response", f"embedding 第 {idx} 条为空向量")
        out: list[float] = []
        for v in vec:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise EmbeddingError("bad_response", f"embedding 第 {idx} 条含非数值")
            fv = float(v)
            if not math.isfinite(fv):  # 拒绝 NaN/Inf
                raise EmbeddingError("bad_response", f"embedding 第 {idx} 条含非有限值")
            out.append(fv)
        if dims is None:
            dims = len(out)
        elif len(out) != dims:
            raise EmbeddingError(
                "bad_response",
                f"embedding 维度不一致(期望 {dims},第 {idx} 条为 {len(out)})",
            )
        vectors[idx] = out
    if dims is None or dims <= 0:
        raise EmbeddingError("bad_response", "embedding 维度无效")
    return vectors  # type: ignore[return-value]


class OpenAICompatEmbeddingAdapter:
    """OpenAI 兼容 embeddings 适配器(cloud/local 共用)。

    - 密钥只放在请求头,不写入日志/异常/记录;
    - 发送文本在请求边界统一脱敏(调用方已脱敏时此处为兜底);
    - 429 记为 rate_limited 且非 retryable:由上层直接降级/失败,不立即重发;
      超时/连接/5xx 记为 retryable,上层可做一次有退避的有限重试;不自动换服务。
    """

    mode = "remote"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        max_batch: int = 16,
        trust_env: bool = True,
    ):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_batch = max_batch
        self.trust_env = trust_env

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], usage=None, model=self.model, dims=0)
        if len(texts) > self.max_batch:
            raise EmbeddingError(
                "bad_request", f"embedding 批量超限({len(texts)} > {self.max_batch})"
            )
        # 最终请求边界统一脱敏:索引文本/提示文本中的凭据不外发
        payload_texts = [sanitize_text(t) for t in texts]
        url = self.base_url.rstrip("/") + "/embeddings"
        body = {"model": self.model, "input": payload_texts}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with httpx.Client(
                timeout=self.timeout_seconds, trust_env=self.trust_env
            ) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise EmbeddingError(
                "timeout", f"embedding 调用超时(>{self.timeout_seconds}s)", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                "connect", f"embedding 连接失败: {sanitize_text(str(exc))}", retryable=True
            ) from exc

        if resp.status_code == 429:
            # 限流不标记 retryable:调用方不得同步立即重发(会放大上游压力),
            # 检索路径直接降级关键词,索引构建直接失败并如实记录调用次数。
            raise EmbeddingError("rate_limited", "embedding 服务限流(HTTP 429)", retryable=False)
        if resp.status_code in (401, 403):
            raise EmbeddingError("auth", "embedding 服务鉴权失败;请检查密钥配置")
        if resp.status_code >= 500:
            raise EmbeddingError(
                "upstream", f"embedding 服务错误(HTTP {resp.status_code})", retryable=True
            )
        if resp.status_code >= 400:
            raise EmbeddingError(
                "bad_request",
                f"embedding 请求被拒绝(HTTP {resp.status_code});请检查端点/模型/参数配置",
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise EmbeddingError("bad_response", "embedding 响应不是 JSON") from exc
        if not isinstance(data, dict):
            raise EmbeddingError("bad_response", "embedding 响应不是对象")

        vectors = _validate_embeddings(payload_texts, data.get("data"))

        usage: dict | None = None
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                k: int(v)
                for k, v in raw_usage.items()
                if isinstance(v, (int, float))
                and not isinstance(v, bool)
                and k in ("prompt_tokens", "total_tokens", "completion_tokens")
            } or None

        return EmbeddingResult(
            vectors=vectors,
            usage=usage,
            model=str(data.get("model", self.model)),
            dims=len(vectors[0]) if vectors else 0,
        )

    def describe(self) -> dict:
        # 只暴露非敏感配置;api_key 只暴露是否已配置
        return {
            "mode": self.mode,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key),
            "timeout_seconds": self.timeout_seconds,
            "max_batch": self.max_batch,
            "trust_env_proxy": self.trust_env,
        }


def build_embedding_adapter(settings: Settings) -> EmbeddingAdapter:
    """按配置构造 embedding 适配器。构造即校验 LOCAL_ONLY 与 local 目标可信(与聊天同一契约)。"""
    mode = settings.embedding_mode
    if mode == "disabled":
        return DisabledEmbeddingAdapter()

    if mode == "cloud":
        if settings.local_only:
            raise EmbeddingError(
                "config", "LOCAL_ONLY=1 禁止 cloud embedding 调用;不允许数据外发"
            )
        if not settings.embedding_base_url:
            raise EmbeddingError("config", "EMBEDDING_MODE=cloud 但未配置 EMBEDDING_BASE_URL")
    elif mode == "local":
        if not settings.embedding_base_url:
            raise EmbeddingError("config", "EMBEDDING_MODE=local 但未配置 EMBEDDING_BASE_URL")
        if not _is_trusted_local_target(settings.embedding_base_url):
            raise EmbeddingError(
                "config",
                "EMBEDDING_MODE=local 的目标地址不是受信任的本机/内网地址;"
                "请由管理员配置正确的内网端点(应用级校验不等于网络隔离)",
            )
    else:
        raise EmbeddingError("config", f"未知 EMBEDDING_MODE: {mode}")

    return OpenAICompatEmbeddingAdapter(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        timeout_seconds=float(settings.embedding_timeout_seconds),
        max_batch=settings.embedding_batch_size,
        # local 直连内网端点,不继承环境代理(防经外部代理中转出网;与聊天同一契约)
        trust_env=(mode != "local"),
    )

"""embedding 适配层测试(第三阶段第二批)。

覆盖:disabled 不产生伪向量、LOCAL_ONLY 拒 cloud、local 目标校验与代理隔离、
返回校验(条数/索引/维度/空向量/非有限值/非数值)、超时/限流/鉴权错误映射、
usage 缺失保持未知、最终请求边界脱敏(整份请求体断言,不只测脱敏函数)。
不调用外部服务;合成请求/响应夹具不代表真实模型能力。
"""

import math

import httpx
import pytest

from app.ai.embeddings import (
    DisabledEmbeddingAdapter,
    EmbeddingError,
    OpenAICompatEmbeddingAdapter,
    build_embedding_adapter,
)
from app.config import Settings


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    """替换 httpx.Client:捕获构造参数与请求体;按脚本返回响应或抛错。"""

    captured: dict = {}
    response: _FakeResp | Exception | None = None

    def __init__(self, **kwargs):
        _FakeClient.captured = dict(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        _FakeClient.captured["url"] = url
        _FakeClient.captured["body"] = json
        _FakeClient.captured["headers"] = headers
        item = _FakeClient.response
        if isinstance(item, Exception):
            raise item
        return item if item is not None else _FakeResp()


def _ok_payload(n=2, dims=4, usage=True):
    data = {
        "model": "emb-model",
        "data": [
            {"index": i, "embedding": [float(i + 1)] * dims} for i in range(n)
        ],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 7, "total_tokens": 7}
    return data


def _adapter(**kw):
    kw.setdefault("base_url", "https://emb.example.com/v1")
    kw.setdefault("model", "emb-model")
    kw.setdefault("api_key", "sk-synthetic-key-not-real")
    return OpenAICompatEmbeddingAdapter(**kw)


@pytest.fixture(autouse=True)
def _fake_httpx(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient.response = _FakeResp(payload=_ok_payload())
    monkeypatch.setattr("app.ai.embeddings.httpx.Client", _FakeClient)
    yield


# ---- 构造与模式 ----


def test_disabled_adapter_never_produces_vectors():
    with pytest.raises(EmbeddingError) as exc:
        DisabledEmbeddingAdapter().embed(["x"])
    assert exc.value.code == "disabled"
    adapter = build_embedding_adapter(Settings(embedding_mode="disabled"))
    assert isinstance(adapter, DisabledEmbeddingAdapter)


def test_local_only_rejects_cloud_embedding(monkeypatch):
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_adapter(
            Settings(
                embedding_mode="cloud",
                embedding_base_url="https://emb.example.com/v1",
                embedding_model="m",
                local_only=True,
            )
        )
    assert exc.value.code == "config"
    assert "LOCAL_ONLY" in exc.value.message


def test_local_mode_untrusted_target_rejected_and_trusted_ignores_env_proxy(monkeypatch):
    with pytest.raises(EmbeddingError) as exc:
        build_embedding_adapter(
            Settings(embedding_mode="local", embedding_base_url="https://public.example.com/v1", embedding_model="m")
        )
    assert exc.value.code == "config"

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.external.example:8080")
    adapter = build_embedding_adapter(
        Settings(embedding_mode="local", embedding_base_url="http://10.1.2.3:8000", embedding_model="m")
    )
    _FakeClient.response = _FakeResp(payload=_ok_payload(n=1))
    result = adapter.embed(["hello"])
    assert _FakeClient.captured.get("trust_env") is False  # local 不经环境代理
    assert len(result.vectors) == 1

    cloud = build_embedding_adapter(
        Settings(embedding_mode="cloud", embedding_base_url="https://emb.example.com/v1", embedding_model="m")
    )
    _FakeClient.response = _FakeResp(payload=_ok_payload(n=1))
    cloud.embed(["hello"])
    assert _FakeClient.captured.get("trust_env") is True  # cloud 保留部署环境代理


# ---- 返回校验(异常结果一律拒绝,不产生可用坏索引) ----


@pytest.mark.parametrize(
    "mutate,keyword",
    [
        (lambda p: p.update(data=p["data"][:1]), "条数"),  # 返回条数不足
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}]), "重复"),  # 索引重复
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 5, "embedding": [2.0]}]), "索引"),  # 越界索引
        (lambda p: p.update(data=[{"embedding": [1.0]}, {"index": 1, "embedding": [2.0]}]), "索引"),  # 缺 index
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 2.0]}]), "维度"),
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": []}]), "空向量"),
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [float("nan")]}]), "有限"),
        (lambda p: p.update(data=[{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": ["x"]}]), "数值"),
        (lambda p: p.update(data="not-a-list"), "data"),
    ],
)
def test_invalid_embedding_payloads_rejected(mutate, keyword):
    payload = _ok_payload(n=2, dims=1)
    mutate(payload)
    _FakeClient.response = _FakeResp(payload=payload)
    with pytest.raises(EmbeddingError) as exc:
        _adapter().embed(["a", "b"])
    assert exc.value.code == "bad_response"
    assert keyword in exc.value.message


def test_index_correspondence_uses_returned_index():
    """索引对应关系:返回乱序但 index 合法时按 index 归位(不按数组顺序错配)。"""
    payload = {
        "model": "emb-model",
        "data": [
            {"index": 1, "embedding": [2.0]},
            {"index": 0, "embedding": [1.0]},
        ],
    }
    _FakeClient.response = _FakeResp(payload=payload)
    result = _adapter().embed(["a", "b"])
    assert result.vectors == [[1.0], [2.0]]  # index=0 对应第一条输入


def test_non_finite_values_rejected_even_if_json_accepts_nan():
    payload = _ok_payload(n=1, dims=2)
    payload["data"][0]["embedding"] = [1.0, math.inf]
    _FakeClient.response = _FakeResp(payload=payload)
    with pytest.raises(EmbeddingError) as exc:
        _adapter().embed(["a"])
    assert "有限" in exc.value.message


# ---- 错误映射与有限重试属性 ----


def test_error_mapping_timeout_rate_limit_auth():
    _FakeClient.response = httpx.TimeoutException("t")
    with pytest.raises(EmbeddingError) as exc:
        _adapter().embed(["a"])
    assert exc.value.code == "timeout" and exc.value.retryable

    _FakeClient.response = _FakeResp(status_code=429)
    with pytest.raises(EmbeddingError) as exc:
        _adapter().embed(["a"])
    # 429 不可重试:调用方直接降级/失败,不得同步立即重发(复审 2026-10-19 P2)
    assert exc.value.code == "rate_limited" and not exc.value.retryable

    _FakeClient.response = _FakeResp(status_code=401)
    with pytest.raises(EmbeddingError) as exc:
        _adapter().embed(["a"])
    assert exc.value.code == "auth" and not exc.value.retryable


def test_usage_missing_stays_unknown():
    _FakeClient.response = _FakeResp(payload=_ok_payload(usage=False))
    result = _adapter().embed(["a", "b"])
    assert result.usage is None  # 缺失保持未知,不虚构
    _FakeClient.response = _FakeResp(payload=_ok_payload(usage=True))
    result = _adapter().embed(["a", "b"])
    assert result.usage == {"prompt_tokens": 7, "total_tokens": 7}


def test_batch_limit_and_empty_input():
    with pytest.raises(EmbeddingError) as exc:
        _adapter(max_batch=2).embed(["a", "b", "c"])
    assert exc.value.code == "bad_request"
    empty = _adapter().embed([])
    assert empty.vectors == [] and empty.usage is None


# ---- 最终请求边界脱敏与密钥不泄露 ----


def test_request_body_sanitized_and_key_not_leaked():
    secret_text = "日志里有 password=EmbSecretZ9! 和 api_key=\"EmbSecretY8!\""
    _FakeClient.response = _FakeResp(payload=_ok_payload(n=1))
    adapter = _adapter()
    adapter.embed([secret_text])

    body = _FakeClient.captured["body"]
    assert "EmbSecretZ9!" not in str(body), "凭据进入了 embedding 最终请求"
    assert "EmbSecretY8!" not in str(body)
    assert "[REDACTED]" in str(body)
    # 密钥只在请求头;错误信息与 describe 不含密钥
    assert _FakeClient.captured["headers"]["Authorization"].startswith("Bearer ")
    described = str(adapter.describe())
    assert "sk-synthetic-key-not-real" not in described

    _FakeClient.response = _FakeResp(status_code=500)
    with pytest.raises(EmbeddingError) as exc:
        adapter.embed([secret_text])
    assert "sk-synthetic-key-not-real" not in exc.value.message
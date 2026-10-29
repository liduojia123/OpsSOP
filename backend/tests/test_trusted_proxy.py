"""可信代理客户端地址解析的回归测试。

直接调用 ASGI 中间件，避免依赖 Starlette TestClient 的 client 参数
（当前锁定的 Starlette 版本不支持自定义来源地址）。
"""

import asyncio

import pytest

from app.net import TrustedProxyMiddleware, parse_trusted_cidrs


def resolve(peer: str, headers: dict[str, str], trusted: str = "172.18.0.0/16") -> str:
    seen: dict[str, tuple[str, int]] = {}

    async def downstream(scope, receive, send):
        seen["client"] = scope["client"]

    middleware = TrustedProxyMiddleware(downstream, parse_trusted_cidrs(trusted))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/who",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": (peer, 43210),
    }
    asyncio.run(middleware(scope, None, None))
    return seen["client"][0]


def test_trusted_peer_uses_leftmost_forwarded_address():
    assert resolve("172.18.0.5", {"X-Forwarded-For": "203.0.113.10, 172.18.0.2"}) == "203.0.113.10"


def test_trusted_peer_falls_back_to_real_ip():
    assert resolve("172.18.0.5", {"X-Real-IP": "198.51.100.7"}) == "198.51.100.7"


def test_untrusted_peer_ignores_forwarded_header():
    assert resolve("192.0.2.50", {"X-Forwarded-For": "203.0.113.10"}) == "192.0.2.50"


def test_all_trusted_chain_keeps_original_client():
    assert resolve("172.18.0.5", {"X-Forwarded-For": "172.18.0.9, 172.18.0.2"}) == "172.18.0.9"


def test_no_proxy_header_uses_peer():
    assert resolve("172.18.0.5", {}) == "172.18.0.5"


def test_empty_trusted_list_ignores_forwarded_header():
    assert resolve("172.18.0.5", {"X-Forwarded-For": "203.0.113.10"}, trusted="") == "172.18.0.5"


def test_invalid_cidr_is_rejected():
    with pytest.raises(ValueError):
        parse_trusted_cidrs("172.18.0.0/16, not-a-cidr")

"""可信代理与真实客户端地址解析。

部署在反向代理之后时，应用收到的连接来源是代理地址。只有来自
`TRUSTED_PROXY_CIDRS` 的请求才允许采信 `X-Forwarded-For` / `X-Real-IP`，
并从代理链右侧向左跳过可信代理，取第一个不可信地址作为客户端地址。
未配置可信代理时忽略转发头，保持直连行为。

该中间件替代 uvicorn 的 `--proxy-headers`（启动脚本已关闭后者），
避免两套解析规则不一致。
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.types import ASGIApp, Receive, Scope, Send

Network = IPv4Network | IPv6Network


def parse_trusted_cidrs(raw: str) -> tuple[Network, ...]:
    """解析逗号分隔的 CIDR；空字符串表示不信任任何代理。"""
    networks: list[Network] = []
    for part in (raw or "").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            networks.append(ip_network(value, strict=False))
        except ValueError as exc:
            raise ValueError(f"TRUSTED_PROXY_CIDRS 含无效网段: {value!r}") from exc
    return tuple(networks)


class TrustedProxyMiddleware:
    def __init__(self, app: ASGIApp, trusted_cidrs: tuple[Network, ...] = ()) -> None:
        self.app = app
        self.networks = trusted_cidrs

    def _trusted(self, host: str) -> bool:
        try:
            address = ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    @staticmethod
    def _header(scope: Scope, name: bytes) -> str:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                return value.decode("latin-1")
        return ""

    def resolve_client(self, peer: str, scope: Scope) -> str:
        """从右向左跳过可信代理，返回最左侧的不可信地址。"""
        forwarded = self._header(scope, b"x-forwarded-for")
        if forwarded:
            chain = [item.strip() for item in forwarded.split(",") if item.strip()]
        else:
            real_ip = self._header(scope, b"x-real-ip").strip()
            chain = [real_ip] if real_ip else []
        if not chain:
            return peer
        chain.append(peer)
        for candidate in reversed(chain):
            if not self._trusted(candidate):
                return candidate
        # 整条链都在可信网段内时，保留最左侧原始地址（常见于内网客户端）
        return chain[0]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client and self.networks:
                peer, port = client[0], client[1]
                if self._trusted(peer):
                    scope["client"] = (self.resolve_client(peer, scope), port)
        await self.app(scope, receive, send)

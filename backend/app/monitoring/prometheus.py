"""Prometheus HTTP API 客户端(只读)。

- 只使用 `/api/v1/query`、`/api/v1/query_range`、`/api/v1/targets`;
- 响应体上限 2MB、序列数上限 50、单序列点数上限 720,防止大查询拖垮服务;
- URL 由管理员配置:必须是 http(s) 绝对地址,不允许内嵌用户名口令;
- 错误统一为 MonitoringError,消息不含凭据。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SERIES = 50
MAX_POINTS = 720
MAX_QUERY_CHARS = 600


class MonitoringError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_prometheus_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        raise MonitoringError("config", "请填写 Prometheus 地址")
    if len(value) > 300:
        raise MonitoringError("config", "Prometheus 地址过长")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise MonitoringError("config", "Prometheus 地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise MonitoringError("config", "Prometheus 地址缺少主机名")
    if parsed.username or parsed.password:
        raise MonitoringError("config", "Prometheus 地址不能包含用户名或口令")
    if parsed.query or parsed.fragment:
        raise MonitoringError("config", "Prometheus 地址不能包含查询参数")
    return value.rstrip("/")


def validate_query(expr: str) -> str:
    value = (expr or "").strip()
    if not value:
        raise MonitoringError("bad_query", "PromQL 不能为空")
    if len(value) > MAX_QUERY_CHARS:
        raise MonitoringError("bad_query", f"PromQL 过长(最多 {MAX_QUERY_CHARS} 字符)")
    if "\n" in value:
        raise MonitoringError("bad_query", "PromQL 不能包含换行")
    return value


@dataclass
class Sample:
    labels: dict[str, str]
    value: float
    timestamp: float


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class QueryResult:
    series: list[Series] = field(default_factory=list)
    truncated: bool = False

    def summary(self, limit: int = 1500) -> str:
        lines: list[str] = []
        for item in self.series:
            labels = ",".join(f"{k}={v}" for k, v in sorted(item.labels.items()) if k != "__name__")
            if item.points:
                values = [v for _, v in item.points]
                last = values[-1]
                lines.append(
                    f"{labels or 'value'}: last={last:.4g} min={min(values):.4g} max={max(values):.4g}"
                )
            else:
                lines.append(f"{labels or 'value'}: empty")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit] + "\n…[已截断]"
        if self.truncated:
            text += "\n[结果已按序列/点数上限截断]"
        return text


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = validate_prometheus_url(base_url)
        self.timeout = max(1.0, min(float(timeout), 30.0))

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                resp = client.get(self.base_url + path, params=params)
                content = resp.content
        except httpx.TimeoutException as exc:
            raise MonitoringError("timeout", "Prometheus 请求超时") from exc
        except httpx.HTTPError as exc:
            raise MonitoringError("connect", "无法连接 Prometheus") from exc
        if len(content) > MAX_RESPONSE_BYTES:
            raise MonitoringError("too_large", "Prometheus 响应过大,请缩小查询范围")
        if resp.status_code in (401, 403):
            raise MonitoringError("auth", "Prometheus 鉴权失败")
        if resp.status_code >= 500:
            raise MonitoringError("upstream", f"Prometheus 错误(HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise MonitoringError("bad_query", f"Prometheus 拒绝了查询(HTTP {resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MonitoringError("bad_response", "Prometheus 返回的不是 JSON") from exc
        if payload.get("status") != "success":
            detail = str(payload.get("error") or "查询失败")[:200]
            raise MonitoringError("bad_query", f"Prometheus 查询失败: {detail}")
        return payload.get("data") or {}

    def query(self, expr: str) -> QueryResult:
        active = validate_query(expr)
        data = self._get("/api/v1/query", {"query": active})
        result = data.get("result") or []
        series: list[Series] = []
        truncated = False
        for item in result:
            if len(series) >= MAX_SERIES:
                truncated = True
                break
            metric = item.get("metric") or {}
            raw = item.get("value") or [0, "NaN"]
            try:
                value = float(raw[1])
            except (TypeError, ValueError, IndexError):
                value = math.nan
            series.append(
                Series(
                    labels={str(k): str(v) for k, v in metric.items()},
                    points=[(float(raw[0]), value)],
                )
            )
        return QueryResult(series=series, truncated=truncated)

    def query_range(self, expr: str, *, minutes: int = 60, step: int = 60) -> QueryResult:
        active = validate_query(expr)
        minutes = max(1, min(int(minutes), 1440))
        step = max(15, min(int(step), 3600))
        # Prometheus API 只接受 RFC3339 或 Unix 时间戳;相对时间("now-30m")会被 400 拒绝
        end_ts = time.time()
        start_ts = end_ts - minutes * 60
        data = self._get(
            "/api/v1/query_range",
            {
                "query": active,
                "start": f"{start_ts:.3f}",
                "end": f"{end_ts:.3f}",
                "step": str(step),
            },
        )
        result = data.get("result") or []
        series: list[Series] = []
        truncated = False
        for item in result:
            if len(series) >= MAX_SERIES:
                truncated = True
                break
            metric = {str(k): str(v) for k, v in (item.get("metric") or {}).items()}
            points: list[tuple[float, float]] = []
            for pair in item.get("values") or []:
                if len(points) >= MAX_POINTS:
                    truncated = True
                    break
                try:
                    points.append((float(pair[0]), float(pair[1])))
                except (TypeError, ValueError, IndexError):
                    continue
            series.append(Series(labels=metric, points=points))
        return QueryResult(series=series, truncated=truncated)

    def targets(self) -> list[dict]:
        data = self._get("/api/v1/targets", {})
        out: list[dict] = []
        for item in data.get("activeTargets") or []:
            labels = item.get("labels") or {}
            out.append(
                {
                    "job": labels.get("job", ""),
                    "instance": labels.get("instance", ""),
                    "host": labels.get("host", ""),
                    "health": item.get("health", ""),
                    "last_error": item.get("lastError", ""),
                    "last_scrape": item.get("lastScrape", ""),
                    "scrape_duration": item.get("lastScrapeDuration"),
                }
            )
        return out

    def ready(self) -> bool:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=True) as client:
                resp = client.get(self.base_url + "/-/ready")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
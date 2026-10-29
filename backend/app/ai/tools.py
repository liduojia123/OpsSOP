"""AI 只读工具:Prometheus 查询与 SSH 只读命令。

- 工具数量固定、参数强校验;AI 不能执行任意命令;
- 每次调用记录审计明细(不保存私钥/凭据);
- 结果截断并脱敏,以 `[evidence: tool:N]` 前缀作为可引用证据;
- 单次分析最多 5 次调用,每用户每分钟 12 次。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.sanitize import sanitize_text
from app.config import Settings
from app.models import AiToolInvocation, MonitoredServer, User
from app.monitoring.defaults import resolve_client
from app.monitoring.prometheus import MonitoringError
from app.monitoring.service import execute_server_template
from app.monitoring.ssh import SshError
from app.monitoring.templates import TemplateError

logger = logging.getLogger("opssop.ai.tools")

MAX_CALLS_PER_ANALYSIS = 5
MAX_RESULT_CHARS = 2000
_RATE_LIMIT = 12
_RATE_WINDOW = 60
_rate_lock = threading.Lock()
_rate_buckets: dict[int, list[float]] = {}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "prometheus_query",
            "description": (
                "查询 Prometheus 指标(PromQL,只读)。用于确认 CPU/内存/磁盘/网络/负载等实时状态与趋势。"
                "返回每个实例的最后值、最小值和最大值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL 表达式,例如 up 或 node_load1"},
                    "minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120,
                        "description": "回看分钟数,默认 15",
                    },
                    "step": {
                        "type": "integer",
                        "minimum": 15,
                        "maximum": 300,
                        "description": "采样步长秒数,默认 60",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_run",
            "description": (
                "在已登记服务器上执行一条内置只读诊断命令(白名单模板)。"
                "可用模板: uptime、memory、disk、load、top、listen_ports、docker_ps、"
                "service_status(参数 unit)、journal(参数 unit/lines)。"
                "只能读取信息,不能执行任意命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "服务器名称或 ID"},
                    "template": {"type": "string", "description": "内置模板 key"},
                    "args": {
                        "type": "object",
                        "description": "模板参数,例如 {\"unit\":\"nginx\",\"lines\":100}",
                    },
                },
                "required": ["server", "template"],
            },
        },
    },
]


class ToolDenied(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class ToolOutcome:
    content: str
    evidence_id: str
    status: str = "ok"


@dataclass
class ToolExecutor:
    db: Session
    user: User
    settings: Settings
    fault_id: int
    used: int = 0
    invocations: list[dict] = field(default_factory=list)
    _evidence: list[tuple[str, str, str]] = field(default_factory=list)

    def available(self) -> bool:
        client = resolve_client(self.db)
        if client is not None:
            return True
        server = self.db.scalar(
            select(MonitoredServer).where(
                MonitoredServer.enabled.is_(True),
                MonitoredServer.private_key_encrypted != "",
            )
        )
        return server is not None

    def _check_rate(self) -> None:
        now = time.monotonic()
        with _rate_lock:
            bucket = [t for t in _rate_buckets.get(self.user.id, []) if now - t < _RATE_WINDOW]
            if len(bucket) >= _RATE_LIMIT:
                _rate_buckets[self.user.id] = bucket
                raise ToolDenied("实时查询过于频繁,请稍后再试")
            bucket.append(now)
            _rate_buckets[self.user.id] = bucket

    def execute(self, name: str, raw_arguments: str | dict | None) -> ToolOutcome:
        if self.used >= MAX_CALLS_PER_ANALYSIS:
            return ToolOutcome(
                content="已达到本次分析的实时查询上限(5 次),请基于现有信息给出结论。",
                evidence_id="",
                status="denied",
            )
        try:
            arguments = raw_arguments
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (ValueError, TypeError) as exc:
            arguments = {"raw": str(raw_arguments)[:200]}
            self._record(name, arguments, "error", "", f"参数解析失败: {type(exc).__name__}")
            return ToolOutcome(content="工具参数解析失败,请使用 JSON 对象。", evidence_id="", status="error")

        self.used += 1
        index = self.used
        started = time.monotonic()
        try:
            self._check_rate()
            if name == "prometheus_query":
                text = self._prometheus(arguments)
            elif name == "ssh_run":
                text = self._ssh(arguments)
            else:
                raise ToolDenied(f"未知工具: {name}")
        except ToolDenied as exc:
            self._record(name, arguments, "denied", "", exc.message, started)
            return ToolOutcome(content=f"工具调用被拒绝: {exc.message}", evidence_id="", status="denied")
        except (MonitoringError, SshError, TemplateError) as exc:
            message = getattr(exc, "message", str(exc))
            self._record(name, arguments, "error", "", message, started)
            return ToolOutcome(content=f"工具执行失败: {message}", evidence_id="", status="error")
        except Exception as exc:  # noqa: BLE001 - 工具失败不中断分析
            logger.exception("工具执行异常: %s", name)
            self._record(name, arguments, "error", "", f"{type(exc).__name__}", started)
            return ToolOutcome(content=f"工具执行异常: {type(exc).__name__}", evidence_id="", status="error")

        evidence_id = f"tool:{index}"
        content = sanitize_text(text)[:MAX_RESULT_CHARS]
        if len(text) > MAX_RESULT_CHARS:
            content += "\n…[结果已截断]"
        header = f"[evidence: {evidence_id}] 实时查询结果({name})"
        body = f"{header}\n{content}"
        self._evidence.append((evidence_id, f"tool:{name}", content))
        self._record(name, arguments, "ok", content, "", started)
        return ToolOutcome(content=body, evidence_id=evidence_id)

    def _prometheus(self, arguments: dict) -> str:
        client = resolve_client(self.db)
        if client is None:
            raise MonitoringError("config", "Prometheus 未启用或未配置")
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise MonitoringError("bad_query", "缺少 query 参数")
        try:
            minutes = int(arguments.get("minutes") or 15)
        except (TypeError, ValueError) as exc:
            raise MonitoringError("bad_query", "minutes 必须是整数") from exc
        try:
            step = int(arguments.get("step") or 60)
        except (TypeError, ValueError) as exc:
            raise MonitoringError("bad_query", "step 必须是整数") from exc
        result = client.query_range(query, minutes=minutes, step=step)
        if not result.series:
            return f"查询 {query} 在最近 {minutes} 分钟没有数据。"
        return f"PromQL: {query}\n{result.summary()}"

    def _ssh(self, arguments: dict) -> str:
        server_key = str(arguments.get("server") or "").strip()
        template = str(arguments.get("template") or "").strip()
        args = arguments.get("args") or {}
        if not server_key:
            raise ToolDenied("缺少 server 参数")
        if not template:
            raise ToolDenied("缺少 template 参数")
        if not isinstance(args, dict):
            raise ToolDenied("args 必须是对象")
        server = None
        if server_key.isdigit():
            server = self.db.get(MonitoredServer, int(server_key))
        if server is None:
            server = self.db.scalar(
                select(MonitoredServer).where(MonitoredServer.name == server_key)
            )
        if server is None or not server.enabled:
            raise ToolDenied(f"未找到可用的服务器: {server_key}")
        result = execute_server_template(self.db, server, template, args, self.settings)
        header = (
            f"服务器: {server.name}({server.host}) 模板: {template} "
            f"退出码: {result['exit_status']}"
        )
        output = result["stdout"].strip() or "(无输出)"
        return f"{header}\n{output}"

    def _record(
        self,
        name: str,
        arguments: dict,
        status: str,
        result: str,
        error: str,
        started: float | None = None,
    ) -> None:
        latency = int((time.monotonic() - started) * 1000) if started else 0
        safe_args = {
            k: (str(v)[:200] if not isinstance(v, (int, float, bool, type(None), dict)) else v)
            for k, v in (arguments or {}).items()
        }
        self.invocations.append(
            {
                "tool": name,
                "arguments": safe_args,
                "status": status,
                "result_excerpt": (result or "")[:500],
                "error": error[:300],
                "latency_ms": latency,
            }
        )

    def evidence(self) -> list[tuple[str, str, str]]:
        return list(self._evidence)

    def persist(self, analysis_id: int) -> None:
        for item in self.invocations:
            self.db.add(
                AiToolInvocation(
                    analysis_id=analysis_id,
                    fault_id=self.fault_id,
                    user_id=self.user.id,
                    tool=item["tool"],
                    arguments=item["arguments"],
                    status=item["status"],
                    result_excerpt=item["result_excerpt"],
                    error=item["error"],
                    latency_ms=item["latency_ms"],
                )
            )
        if self.invocations:
            self.db.commit()


def build_executor(
    db: Session,
    user: User,
    settings: Settings,
    fault_id: int,
    *,
    enabled: bool,
) -> ToolExecutor | None:
    if not enabled:
        return None
    executor = ToolExecutor(db=db, user=user, settings=settings, fault_id=fault_id)
    return executor if executor.available() else None
"""AI 只读工具:本系统业务数据、Prometheus 查询与 SSH 只读命令。

- 工具数量固定、参数强校验;AI 不能执行任意命令;
- 业务数据工具沿用页面权限:运维只看到自己的故障,审核/管理员可见全部;
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

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.sanitize import sanitize_text
from app.config import Settings
from app.models import (
    AiToolInvocation,
    Case,
    CaseStatus,
    Category,
    Fault,
    FaultStatus,
    MonitoredServer,
    Role,
    Sop,
    SopStatus,
    User,
)
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
    {
        "type": "function",
        "function": {
            "name": "list_faults",
            "description": (
                "查询 OpsSOP 本系统内的故障记录(只读,仅返回当前用户有权访问的数据)。"
                "用户询问“有哪些待解决的问题/未解决故障/我的故障/某个服务的故障”时使用。"
                "默认 status=unresolved(草稿与处理中,不含已解决);可按关键词筛选。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["unresolved", "draft", "open", "resolved", "all"],
                        "description": "故障状态,默认 unresolved",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "可选关键词,匹配标题或服务名",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "最多返回条数,默认 10",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": (
                "查询当前用户的待办汇总:本人未解决故障、被退回的案例/SOP;"
                "审核人员与管理员还包括待审核案例与待审核 SOP。"
                "用户询问“我的待办/有什么要我处理/待审核什么”时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "每类最多返回条数,默认 10",
                    }
                },
            },
        },
    },
]


def _text(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _fmt_time(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _limit(raw, *, default: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


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
    fault_id: int | None = None
    conversation_id: int | None = None
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

    def specs(self) -> list[dict]:
        """按上下文返回可用工具:业务数据仅对话;监控工具需已配置数据源或服务器。"""
        names: set[str] = set()
        if self.conversation_id is not None:
            names.update({"list_faults", "list_todos"})
        if self.available():
            names.update({"prometheus_query", "ssh_run"})
        return [spec for spec in TOOL_SPECS if spec["function"]["name"] in names]

    def _allowed(self) -> set[str]:
        """后端实际允许调用的工具:监控工具在分析和对话中都允许(未配置时由工具自身报错)。"""
        names = {"prometheus_query", "ssh_run"}
        if self.conversation_id is not None:
            names.update({"list_faults", "list_todos"})
        return names

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
            if name not in self._allowed():
                raise ToolDenied(f"未知工具或当前场景不可用: {name}")
            if name == "prometheus_query":
                text = self._prometheus(arguments)
            elif name == "ssh_run":
                text = self._ssh(arguments)
            elif name == "list_faults":
                text = self._list_faults(arguments)
            elif name == "list_todos":
                text = self._list_todos(arguments)
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

    def _list_faults(self, arguments: dict) -> str:
        """故障清单:权限与故障列表页一致(运维只看本人,审核/管理看全部)。"""
        status = str(arguments.get("status") or "unresolved").strip().lower()
        if status not in {"unresolved", "draft", "open", "resolved", "all"}:
            raise ToolDenied("status 只支持 unresolved/draft/open/resolved/all")
        keyword = str(arguments.get("keyword") or "").strip()[:80]
        limit = _limit(arguments.get("limit"), default=10, maximum=20)

        with self.db.begin_nested():
            stmt = select(Fault)
            if self.user.role == Role.operator:
                stmt = stmt.where(Fault.created_by == self.user.id)
            if status == "unresolved":
                stmt = stmt.where(Fault.status != FaultStatus.resolved)
            elif status != "all":
                stmt = stmt.where(Fault.status == FaultStatus(status))
            if keyword:
                like = f"%{keyword}%"
                stmt = stmt.where(or_(Fault.title.ilike(like), Fault.service.ilike(like)))
            total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            rows = list(self.db.scalars(stmt.order_by(Fault.updated_at.desc()).limit(limit)))
            categories = {row.id: row.name for row in self.db.scalars(select(Category))}

        if total == 0:
            return "当前没有符合条件且有权访问的故障。"
        scope = "本人" if self.user.role == Role.operator else "全部用户"
        lines = [
            f"故障清单(状态={status},范围={scope},共 {total} 条,显示最近 {len(rows)} 条):"
        ]
        for row in rows:
            parts = [f"#{row.id}", f"[{_text(row.status)}]", (row.title or "")[:80]]
            if row.service:
                parts.append(f"服务={row.service}")
            if row.environment:
                parts.append(f"环境={row.environment}")
            if row.category_id in categories:
                parts.append(f"分类={categories[row.category_id]}")
            parts.append(f"更新={_fmt_time(row.updated_at)}")
            lines.append(" · ".join(parts))
        return "\n".join(lines)

    def _list_todos(self, arguments: dict) -> str:
        """待办汇总:与工作台一致(本人未解决故障 + 被退回;审核/管理含待审核)。"""
        limit = _limit(arguments.get("limit"), default=10, maximum=20)
        reviewer = self.user.role in (Role.reviewer, Role.admin)

        with self.db.begin_nested():
            my_fault_total = self.db.scalar(
                select(func.count(Fault.id)).where(
                    Fault.created_by == self.user.id,
                    Fault.status != FaultStatus.resolved,
                )
            ) or 0
            my_faults = list(
                self.db.scalars(
                    select(Fault)
                    .where(
                        Fault.created_by == self.user.id,
                        Fault.status != FaultStatus.resolved,
                    )
                    .order_by(Fault.updated_at.desc())
                    .limit(limit)
                )
            )
            returned_case_total = self.db.scalar(
                select(func.count(Case.id)).where(
                    Case.created_by == self.user.id,
                    Case.status == CaseStatus.returned,
                )
            ) or 0
            returned_cases = list(
                self.db.scalars(
                    select(Case)
                    .where(
                        Case.created_by == self.user.id,
                        Case.status == CaseStatus.returned,
                    )
                    .order_by(Case.updated_at.desc())
                    .limit(limit)
                )
            )
            returned_sop_total = self.db.scalar(
                select(func.count(Sop.id)).where(
                    Sop.created_by == self.user.id,
                    Sop.status == SopStatus.returned,
                )
            ) or 0
            returned_sops = list(
                self.db.scalars(
                    select(Sop)
                    .where(
                        Sop.created_by == self.user.id,
                        Sop.status == SopStatus.returned,
                    )
                    .order_by(Sop.updated_at.desc())
                    .limit(limit)
                )
            )
            pending_case_total = 0
            pending_cases: list[Case] = []
            pending_sop_total = 0
            pending_sops: list[Sop] = []
            if reviewer:
                pending_case_total = self.db.scalar(
                    select(func.count(Case.id)).where(Case.status == CaseStatus.pending_review)
                ) or 0
                pending_cases = list(
                    self.db.scalars(
                        select(Case)
                        .where(Case.status == CaseStatus.pending_review)
                        .order_by(Case.updated_at)
                        .limit(limit)
                    )
                )
                pending_sop_total = self.db.scalar(
                    select(func.count(Sop.id)).where(Sop.status == SopStatus.submitted)
                ) or 0
                pending_sops = list(
                    self.db.scalars(
                        select(Sop)
                        .where(Sop.status == SopStatus.submitted)
                        .order_by(Sop.updated_at)
                        .limit(limit)
                    )
                )

        lines = ["待办汇总:", f"我的未解决故障: {my_fault_total} 条"]
        for row in my_faults:
            lines.append(
                f"  #{row.id} [{_text(row.status)}] {(row.title or '')[:80]}"
                f" · 更新={_fmt_time(row.updated_at)}"
            )
        if reviewer:
            lines.append(f"待我审核的案例: {pending_case_total} 条")
            for row in pending_cases:
                lines.append(
                    f"  案例 #{row.id} {row.title[:80]} · 更新={_fmt_time(row.updated_at)}"
                )
            lines.append(f"待我审核的 SOP: {pending_sop_total} 条")
            for row in pending_sops:
                lines.append(
                    f"  SOP #{row.id} v{row.version} {row.title[:80]}"
                    f" · 更新={_fmt_time(row.updated_at)}"
                )
        lines.append(f"被退回给我的案例: {returned_case_total} 条")
        for row in returned_cases:
            lines.append(f"  案例 #{row.id} {row.title[:80]}")
        lines.append(f"被退回给我的 SOP: {returned_sop_total} 条")
        for row in returned_sops:
            lines.append(f"  SOP #{row.id} v{row.version} {row.title[:80]}")
        return "\n".join(lines)

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
                    conversation_id=self.conversation_id,
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
    fault_id: int | None = None,
    *,
    enabled: bool,
    conversation_id: int | None = None,
) -> ToolExecutor | None:
    executor = ToolExecutor(
        db=db,
        user=user,
        settings=settings,
        fault_id=fault_id,
        conversation_id=conversation_id,
    )
    if conversation_id is not None:
        # 对话:本系统业务数据工具始终可用;监控工具由 specs() 按配置过滤
        return executor
    if not enabled:
        return None
    return executor if executor.available() else None
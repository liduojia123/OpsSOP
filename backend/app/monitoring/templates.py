"""内置只读诊断命令模板与参数校验。

模板命令固定(由代码种子写入数据库,界面只允许启停);参数按 schema 强校验,
渲染使用 `string.Template` 并拒绝换行/残留占位符,避免命令注入。
"""

from __future__ import annotations

import re
import shlex
from string import Template

UNIT_PATTERN = r"^[A-Za-z0-9@._:-]{1,64}$"

BUILTIN_TEMPLATES: list[dict] = [
    {
        "key": "uptime",
        "name": "运行时长与平均负载",
        "command": "uptime",
        "param_schema": {},
        "description": "系统已运行时间与 1/5/15 分钟平均负载",
    },
    {
        "key": "memory",
        "name": "内存使用",
        "command": "free -m",
        "param_schema": {},
        "description": "内存与 swap 使用情况(MB)",
    },
    {
        "key": "disk",
        "name": "磁盘使用",
        "command": "df -h",
        "param_schema": {},
        "description": "各挂载点容量与使用率",
    },
    {
        "key": "load",
        "name": "负载明细",
        "command": "cat /proc/loadavg",
        "param_schema": {},
        "description": "内核 loadavg 与运行/总进程数",
    },
    {
        "key": "top",
        "name": "进程快照",
        "command": "top -bn1 | head -n 30",
        "param_schema": {},
        "description": "按 CPU 排序的进程快照(单次,非交互)",
    },
    {
        "key": "listen_ports",
        "name": "监听端口",
        "command": "ss -ltn",
        "param_schema": {},
        "description": "TCP 监听端口",
    },
    {
        "key": "docker_ps",
        "name": "容器状态",
        "command": "docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}'",
        "param_schema": {},
        "description": "运行中的容器(需要账号有 docker 权限)",
    },
    {
        "key": "service_status",
        "name": "服务状态",
        "command": "systemctl status $unit --no-pager -n 20",
        "param_schema": {
            "unit": {"required": True, "pattern": UNIT_PATTERN, "max": 64},
        },
        "description": "指定 systemd 服务状态与最近 20 行日志",
    },
    {
        "key": "journal",
        "name": "服务日志",
        "command": "journalctl -u $unit -n $lines --no-pager",
        "param_schema": {
            "unit": {"required": True, "pattern": UNIT_PATTERN, "max": 64},
            "lines": {"type": "int", "min": 20, "max": 200, "default": 100},
        },
        "description": "指定 systemd 服务最近日志(20-200 行)",
    },
]


class TemplateError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def builtin_template(key: str) -> dict | None:
    return next((t for t in BUILTIN_TEMPLATES if t["key"] == key), None)


def validate_args(schema: dict, args: dict | None) -> dict[str, str]:
    """按 schema 校验并归一化参数;返回可直接代入模板的字符串值。"""
    values: dict[str, str] = {}
    for name, rule in (schema or {}).items():
        raw = (args or {}).get(name)
        if raw is None or raw == "":
            if rule.get("required"):
                raise TemplateError(f"缺少参数: {name}")
            if "default" in rule:
                values[name] = str(rule["default"])
            continue
        if rule.get("type") == "int":
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise TemplateError(f"参数 {name} 必须是整数") from exc
            low, high = rule.get("min"), rule.get("max")
            if low is not None and number < low:
                raise TemplateError(f"参数 {name} 不能小于 {low}")
            if high is not None and number > high:
                raise TemplateError(f"参数 {name} 不能大于 {high}")
            values[name] = str(number)
            continue
        text = str(raw)
        pattern = rule.get("pattern")
        if pattern and not re.fullmatch(pattern, text):
            raise TemplateError(f"参数 {name} 格式不合法")
        if rule.get("max") and len(text) > int(rule["max"]):
            raise TemplateError(f"参数 {name} 过长")
        values[name] = text
    unknown = set(args or {}) - set((schema or {}).keys())
    if unknown:
        raise TemplateError(f"不支持的参数: {', '.join(sorted(unknown))}")
    return values


def render_command(command: str, schema: dict, args: dict | None) -> str:
    values = validate_args(schema, args)
    try:
        rendered = Template(command).substitute(values)
    except (KeyError, ValueError) as exc:
        raise TemplateError(f"模板参数缺失: {exc}") from exc
    if "\n" in rendered or "\r" in rendered:
        raise TemplateError("命令不能包含换行")
    if "$" in rendered:
        raise TemplateError("命令包含未替换的占位符")
    return rendered


def quote_for_shell(command: str) -> str:
    """模板命令已是固定字符串,这里只做最小防护:用于展示/记录。"""
    return shlex.quote(command)
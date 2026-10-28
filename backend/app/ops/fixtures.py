"""演练夹具:通过真实业务 API 创建账号/故障/案例/SOP 版本链/未发布与越权对照。

只有初始管理员由应用启动 seed 创建;本模块其余数据全部走 HTTP 业务 API,
不直接写业务表。返回的密码只留在进程内用于同一轮 HTTP 登录/恢复后续办,
不写入公开报告。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .common import OpsError, safe_secret
from .httpclient import HttpClient


@dataclass
class DrillFixtures:
    run_id: str
    marker: str
    users: dict[str, dict] = field(default_factory=dict)
    category_id: int | None = None
    fault_id: int | None = None
    record_id: int | None = None
    case_id: int | None = None
    sop_v1_id: int | None = None
    sop_v2_id: int | None = None
    sop_doc_id: str = ""
    draft_sop_id: int | None = None
    other_fault_id: int | None = None
    other_case_id: int | None = None
    other_draft_sop_id: int | None = None
    feedback_id: int | None = None
    titles: dict[str, str] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        """报告用最小标识,不含口令/Token/业务全文。"""
        return {
            "run_id": self.run_id,
            "marker": self.marker,
            "users": {
                name: {"id": info.get("id"), "role": info.get("role")}
                for name, info in self.users.items()
            },
            "category_id": self.category_id,
            "fault_id": self.fault_id,
            "case_id": self.case_id,
            "sop_v1_id": self.sop_v1_id,
            "sop_v2_id": self.sop_v2_id,
            "sop_doc_id": self.sop_doc_id,
            "draft_sop_id": self.draft_sop_id,
            "other_fault_id": self.other_fault_id,
            "other_case_id": self.other_case_id,
            "other_draft_sop_id": self.other_draft_sop_id,
            "feedback_id": self.feedback_id,
            "titles": self.titles,
        }


def _category_id(client: HttpClient, name: str = "Kubernetes") -> int:
    resp = client.get("/api/categories")
    for row in resp.body or []:
        if row.get("name") == name:
            return int(row["id"])
    raise OpsError(f"缺少系统分类: {name}", stage="fixtures")


def _find_one(client: HttpClient, path: str, marker: str, key: str = "title") -> dict:
    resp = client.get(path)
    matches = [row for row in (resp.body or []) if marker in str(row.get(key, ""))]
    if len(matches) != 1:
        raise OpsError(
            f"期望按标记找到 1 条记录,实际 {len(matches)}: {path}", stage="fixtures"
        )
    return matches[0]


def _sop_payload(marker: str, category_id: int, suffix: str) -> dict:
    return {
        "title": f"[{marker}] OOMKilled 处理 SOP {suffix}",
        "category_id": category_id,
        "tags": [marker, "drill"],
        "applicable_env": "drill / checkout / 1.0",
        "symptom": "Pod OOMKilled,内存持续增长",
        "preconditions": "已确认 Pod 因内存限制被杀",
        "check_steps": "kubectl describe pod <pod>;检查 limits.memory 与近 24h 内存曲线",
        "handling_steps": "调整 Deployment limits.memory 并滚动重启",
        "recovery_verification": "Pod Running,连续 30 分钟无 OOMKilled",
        "rollback_steps": "恢复旧 limits.memory 并重新滚动",
        "notes": "演练合成内容,不是真实生产根因",
        "source_case_id": None,
        "maintainer_id": None,
    }


def seed_drill_data(
    client: HttpClient,
    *,
    run_id: str,
    admin_username: str,
    admin_password: str,
) -> DrillFixtures:
    marker = f"DRILL-{run_id}"
    fx = DrillFixtures(run_id=run_id, marker=marker)

    admin = HttpClient(client.base_url)
    admin.login(admin_username, admin_password)
    fx.category_id = _category_id(admin)

    specs = [
        ("operator1", f"drillop1{run_id}", "演练运维一", "operator"),
        ("operator2", f"drillop2{run_id}", "演练运维二", "operator"),
        ("reviewer", f"drillrev{run_id}", "演练审核", "reviewer"),
    ]
    for key, username, display_name, role in specs:
        password = safe_secret(24)
        resp = admin.post(
            "/api/users",
            {
                "username": username,
                "password": password,
                "display_name": display_name,
                "role": role,
            },
            expected=(201,),
        )
        fx.users[key] = {
            "id": resp.body.get("id"),
            "username": username,
            "password": password,
            "role": role,
        }

    op1 = HttpClient(client.base_url)
    op1.login(fx.users["operator1"]["username"], fx.users["operator1"]["password"])
    op2 = HttpClient(client.base_url)
    op2.login(fx.users["operator2"]["username"], fx.users["operator2"]["password"])
    reviewer = HttpClient(client.base_url)
    reviewer.login(fx.users["reviewer"]["username"], fx.users["reviewer"]["password"])

    fault_payload = {
        "title": f"[{marker}] OOMKilled 演练故障",
        "description": "checkout Pod OOMKilled;内存限制不足;演练合成输入",
        "environment": "drill",
        "service": "checkout",
        "component_version": "1.0",
        "recent_changes": "演练变更:调整资源限制",
        "error_logs": "Reason: OOMKilled",
        "attempted_actions": "已查看 Pod 状态与事件",
        "category_id": fx.category_id,
        "tags": [marker, "drill", "oom"],
        "status": "open",
    }
    fault = op1.post("/api/faults", fault_payload, expected=(201,)).body
    fx.fault_id = int(fault["id"])
    fx.titles["fault"] = fault["title"]

    record = op1.post(
        f"/api/faults/{fx.fault_id}/records",
        {"kind": "check_result", "content": "演练检查:kubectl top pod 显示内存接近 limits"},
        expected=(201,),
    ).body
    fx.record_id = int(record["id"])

    op1.post(
        f"/api/faults/{fx.fault_id}/resolve",
        {
            "actual_root_cause": "演练合成:limits.memory 配置过低导致 OOMKilled",
            "handling_steps": "演练合成:上调 limits.memory 并滚动重启",
            "recovery_verification": "演练合成:Pod Running 30 分钟无重启",
        },
        expected=(200,),
    )
    case_row = _find_one(
        op1,
        f"/api/cases?q={urllib.parse.quote(marker)}&status=pending_review",
        marker,
    )
    fx.case_id = int(case_row["id"])
    reviewer.post(
        f"/api/cases/{fx.case_id}/approve",
        {"comment": "演练合成审核通过"},
        expected=(200,),
        version=case_row["updated_at"],
    )

    draft = op1.post(
        f"/api/cases/{fx.case_id}/generate-sop", expected=(201,)
    ).body
    fx.sop_doc_id = draft["doc_id"]
    v1_id = int(draft["id"])
    payload = _sop_payload(marker, fx.category_id, "v1")
    payload.update(
        {
            "source_case_id": fx.case_id,
            "maintainer_id": fx.users["operator1"]["id"],
        }
    )
    v1_draft = op1.patch(f"/api/sops/{v1_id}", payload, version=draft["updated_at"]).body
    v1_submitted = op1.post(f"/api/sops/{v1_id}/submit", expected=(200,), version=v1_draft["updated_at"]).body
    v1 = reviewer.post(
        f"/api/sops/{v1_id}/approve",
        {"comment": "演练合成发布 v1"},
        expected=(200,),
        version=v1_submitted["updated_at"],
    ).body
    fx.sop_v1_id = int(v1["id"])

    v2_draft = op1.post(f"/api/sops/{v1_id}/new-version", expected=(201,), version=v1["updated_at"]).body
    v2_id = int(v2_draft["id"])
    v2_draft = op1.patch(
        f"/api/sops/{v2_id}",
        {
            "check_steps": "演练 v2:核对 limits.memory 与 HPA 配置",
            "handling_steps": "演练 v2:调整 limits.memory 并观察 30 分钟",
            "rollback_steps": "演练 v2:恢复旧 limits.memory",
        },
        version=v2_draft["updated_at"],
    ).body
    v2_submitted = op1.post(f"/api/sops/{v2_id}/submit", expected=(200,), version=v2_draft["updated_at"]).body
    v2 = reviewer.post(
        f"/api/sops/{v2_id}/approve",
        {"comment": "演练合成发布 v2"},
        expected=(200,),
        version=v2_submitted["updated_at"],
    ).body
    fx.sop_v2_id = int(v2["id"])
    if v2.get("doc_id") != fx.sop_doc_id:
        raise OpsError("SOP v2 doc_id 与来源不一致", stage="fixtures")

    draft_payload = _sop_payload(marker, fx.category_id, "未发布草稿")
    draft_payload["title"] = f"[{marker}] 未发布草稿 SOP"
    draft_payload["source_case_id"] = fx.case_id
    draft_payload["maintainer_id"] = fx.users["operator1"]["id"]
    draft = op1.post("/api/sops", draft_payload, expected=(201,)).body
    fx.draft_sop_id = int(draft["id"])

    other_fault = op2.post(
        "/api/faults",
        {
            **fault_payload,
            "title": f"[{marker}] 其他运维待审故障",
            "description": "演练合成:其他运维的待审材料",
            "tags": [marker, "drill", "other"],
        },
        expected=(201,),
    ).body
    fx.other_fault_id = int(other_fault["id"])
    op2.post(
        f"/api/faults/{fx.other_fault_id}/resolve",
        {
            "actual_root_cause": "演练合成:其他运维待审根因",
            "handling_steps": "演练合成:其他运维处理",
            "recovery_verification": "演练合成:其他运维恢复验证",
        },
        expected=(200,),
    )
    other_case = _find_one(
        op2,
        f"/api/cases?q={urllib.parse.quote(marker)}&status=pending_review",
        marker,
    )
    if int(other_case["id"]) == fx.case_id:
        raise OpsError("其他运维待审案例定位错误", stage="fixtures")
    fx.other_case_id = int(other_case["id"])

    other_payload = _sop_payload(marker, fx.category_id, "其他运维草稿")
    other_payload["title"] = f"[{marker}] 其他运维草稿 SOP"
    other_payload["maintainer_id"] = fx.users["operator2"]["id"]
    other_draft = op2.post("/api/sops", other_payload, expected=(201,)).body
    fx.other_draft_sop_id = int(other_draft["id"])

    feedback = op1.post(
        f"/api/sops/{fx.sop_v2_id}/feedback",
        {"kind": "effective", "comment": "演练合成:已按 SOP 验证有效"},
        expected=(201,),
    ).body
    fx.feedback_id = int(feedback["id"])

    # 供后续 HTTP 登录恢复库使用(口令只留在内存,不进入 public())
    fx.passwords = {
        name: info["password"] for name, info in fx.users.items()
    }
    return fx

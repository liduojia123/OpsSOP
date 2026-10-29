# -*- coding: utf-8 -*-
"""基础业务闭环端到端验证脚本(仅用标准库)。

[注意] 本脚本会向目标环境写入故障/案例/SOP 测试材料,请指向独立测试环境。

用法(凭据必须显式提供,不内置示例口令):
  export E2E_BASE_URL=http://localhost:8000          # 可指向独立测试环境
  export E2E_OPERATOR_USER=operator
  export E2E_OPERATOR_PASSWORD=...
  export E2E_REVIEWER_USER=reviewer
  export E2E_REVIEWER_PASSWORD=...
  export E2E_ADMIN_USER=admin
  export E2E_ADMIN_PASSWORD=...
  python scripts/e2e_loop.py

每次运行用唯一标记 [e2e:<run_id>] 命名记录,可重复执行;断言按
fault_id / 运行标记精确匹配,不假定环境里只有一条待审核数据。
覆盖: 登录 → 建故障 → 追加记录 → 确认解决 → 越权被拒 → 案例审核 →
      SOP 草稿 → 提交 → 发布 → 新版本 → 旧版转历史 → 工作台 → 审计
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
OP_USER = os.environ.get("E2E_OPERATOR_USER", "operator")
OP_PASS = os.environ.get("E2E_OPERATOR_PASSWORD")
RV_USER = os.environ.get("E2E_REVIEWER_USER", "reviewer")
RV_PASS = os.environ.get("E2E_REVIEWER_PASSWORD")
ADM_USER = os.environ.get("E2E_ADMIN_USER", "admin")
ADM_PASS = os.environ.get("E2E_ADMIN_PASSWORD")

FAILS = []
RUN_ID = uuid.uuid4().hex[:8]


def require_credentials():
    missing = [
        name
        for name, val in [
            ("E2E_OPERATOR_PASSWORD", OP_PASS),
            ("E2E_REVIEWER_PASSWORD", RV_PASS),
            ("E2E_ADMIN_PASSWORD", ADM_PASS),
        ]
        if not val
    ]
    if missing:
        print("缺少环境变量(脚本不内置示例口令): " + ", ".join(missing))
        print("请指向独立测试环境并显式提供凭据,例如:")
        print("  E2E_BASE_URL=http://localhost:8000 E2E_OPERATOR_PASSWORD=... \\")
        print("  E2E_REVIEWER_PASSWORD=... E2E_ADMIN_PASSWORD=... python scripts/e2e_loop.py")
        sys.exit(2)


def req(method, path, token=None, body=None, version=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if version is not None:
        r.add_header("X-Expected-Updated-At", version)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(r, data) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, {"detail": raw.decode("utf-8", "replace")}


def check(name, cond, info=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {info}")
        FAILS.append(name)


def login(username, password):
    status, data = req("POST", "/api/auth/login", body={"username": username, "password": password})
    assert status == 200, f"login {username} failed: {data}"
    return data["token"]


def main():
    require_credentials()
    print(f"目标: {BASE}  本次运行标记: [e2e:{RUN_ID}]")
    print("[注意] 将向目标环境写入带运行标记的测试材料,请确认这是独立测试环境")

    print("== 0. 登录三个角色 ==")
    op = login(OP_USER, OP_PASS)
    rv = login(RV_USER, RV_PASS)
    adm = login(ADM_USER, ADM_PASS)
    check("登录 operator/reviewer/admin", True)

    title = f"[e2e:{RUN_ID}] Pod 频繁 OOMKilled"

    print("== 1. 运维创建故障 ==")
    st, fault = req("POST", "/api/faults", op, {
        "title": title,
        "description": "checkout 服务 pod 被 OOMKilled 重启",
        "environment": "prod",
        "service": "checkout",
        "component_version": "1.8.2",
        "error_logs": "Reason: OOMKilled exit code 137",
        "recent_changes": "昨日发布 v1.8.2",
        "occurred_at": "2026-10-13T08:00:00Z",
        "tags": ["e2e", RUN_ID],
        "status": "open",
    })
    check("创建故障", st == 201, str(fault))
    fid = fault["id"]

    print("== 2. 追加检查记录 ==")
    st, rec = req("POST", f"/api/faults/{fid}/records", op, {
        "kind": "check_result",
        "content": "kubectl top 显示内存接近 limits",
    })
    check("追加检查记录", st == 201, str(rec))

    print("== 3. 人工确认解决(生成待审核案例)==")
    st, detail = req("POST", f"/api/faults/{fid}/resolve", op, {
        "actual_root_cause": "limits.memory 设置过低",
        "handling_steps": "调整 limits 并重启部署",
        "recovery_verification": "稳定运行 30 分钟无重启",
    })
    check("确认解决", st == 200 and detail["status"] == "resolved", str(detail))
    check("人工根因已保存", detail["actual_root_cause"] == "limits.memory 设置过低")

    st, cases = req("GET", "/api/cases?status=pending_review", op)
    mine = [c for c in (cases or []) if c.get("fault_id") == fid]
    check(
        "生成待审核案例(按 fault_id 匹配)",
        st == 200 and len(mine) == 1 and mine[0]["confirmed_root_cause"] == "limits.memory 设置过低",
        f"fault_id={fid} matched={len(mine)}",
    )
    cid = mine[0]["id"]

    print("== 4. 越权校验 ==")
    st, _ = req("POST", f"/api/cases/{cid}/approve", op, {"comment": "x"}, mine[0]["updated_at"])
    check("运维审核案例被拒(403)", st == 403, f"got {st}")
    st, _ = req("POST", "/api/faults", rv, {"title": f"[e2e:{RUN_ID}] 审核员建故障", "status": "draft"})
    check("审核员创建故障被拒(403)", st == 403, f"got {st}")
    st, _ = req("GET", "/api/users", op)
    check("运维查看账号列表被拒(403)", st == 403, f"got {st}")

    print("== 5. 审核通过案例 ==")
    st, case = req("POST", f"/api/cases/{cid}/approve", rv, {"comment": "证据充分"}, mine[0]["updated_at"])
    check("案例审核通过", st == 200 and case["status"] == "approved", str(case))

    print("== 6. 生成 SOP 草稿 → 提交 ==")
    st, sop = req("POST", f"/api/cases/{cid}/generate-sop", op)
    check("从案例生成 SOP 草稿", st == 201 and sop["status"] == "draft" and sop["source_case_id"] == cid, str(sop))
    sid = sop["id"]
    st, sop = req("PATCH", f"/api/sops/{sid}", op, {
        "title": f"[e2e:{RUN_ID}] Kubernetes Pod OOMKilled 排查与处理",
        "check_steps": "1. kubectl describe pod\n2. 检查 limits",
        "preconditions": "kubectl 只读权限",
        "rollback_steps": "回滚到原 limits",
        "notes": "先在测试环境验证",
    }, sop["updated_at"])
    check("补全 SOP", st == 200, str(sop))
    st, sop = req("POST", f"/api/sops/{sid}/submit", op, version=sop["updated_at"])
    check("提交 SOP", st == 200 and sop["status"] == "submitted", str(sop))

    print("== 7. 发布 SOP ==")
    st, sop = req("POST", f"/api/sops/{sid}/approve", rv, {"comment": "通过"}, sop["updated_at"])
    check("发布 SOP", st == 200 and sop["status"] == "published" and sop["published_at"], str(sop))

    print("== 8. 修改已发布 SOP → 新版本,旧版本转历史 ==")
    st, v2 = req("POST", f"/api/sops/{sid}/new-version", op, version=sop["updated_at"])
    check("生成新版本草稿", st == 201 and v2["version"] == 2 and v2["status"] == "draft", str(v2))
    st, v1 = req("GET", f"/api/sops/{sid}", op)
    check("新草稿存在时旧版仍有效", st == 200 and v1["status"] == "published", str(v1))
    _, v2 = req("POST", f"/api/sops/{v2['id']}/submit", op, version=v2["updated_at"])
    st, v2 = req("POST", f"/api/sops/{v2['id']}/approve", rv, {"comment": "ok"}, v2["updated_at"])
    check("发布 v2", st == 200 and v2["status"] == "published")
    st, v1 = req("GET", f"/api/sops/{sid}", op)
    check("v1 自动转为历史版本", v1["status"] == "superseded", str(v1))

    print("== 9. 同 doc 未发布时禁止再开新版本 ==")
    st, v3 = req("POST", f"/api/sops/{v2['id']}/new-version", op, version=v2["updated_at"])
    # v2 是 published,允许开 v3;此时同 doc 已有未发布草稿,再从 v2 派生应 409
    if st == 201:
        st2, _ = req("POST", f"/api/sops/{v2['id']}/new-version", op, version=v2["updated_at"])
        check("已有未发布草稿时再开新版被拒(409)", st2 == 409, f"got {st2}")
    else:
        check("已有未发布草稿时再开新版被拒(409)", False, f"unexpected {st}")

    print("== 10. 工作台与审计 ==")
    st, wb = req("GET", "/api/workbench", rv)
    check("本次案例已不在待审核队列", all(c.get("fault_id") != fid for c in wb["pending_case_reviews"]))
    check("工作台最近 SOP 含已发布", any(s["status"] == "published" for s in wb["recent_sops"]))
    check("工作台有效 SOP 总数 >= 最近列表", wb.get("published_sop_count", 0) >= len(wb["recent_sops"]))
    st, events = req("GET", "/api/audit?limit=200", rv)
    actions = [e["action"] for e in events]
    for a in ["fault.resolve", "case.approve", "sop.publish", "sop.new_version"]:
        check(f"审计记录 {a}", a in actions, str(actions[:10]))

    print("== 11. 分类管理(管理员)==")
    cat_name = f"[e2e:{RUN_ID}] 自定义分类"
    st, cat = req("POST", "/api/categories", adm, {"name": cat_name, "description": "测试"})
    check("管理员创建分类", st == 201, str(cat))
    st, _ = req("PATCH", f"/api/categories/{cat['id']}", adm, {"name": cat_name + "B"})
    check("管理员修改分类", st == 200)
    st, _ = req("DELETE", f"/api/categories/{cat['id']}", adm)
    check("管理员删除分类", st == 204, f"got {st}")

    print("== 12. AI 状态(基础业务闭环使用 disabled 模式)==")
    st, ai = req("GET", "/api/system/ai-status", op)
    check("AI 状态可读", st == 200 and ai["ai_mode"] == "disabled", str(ai))

    print()
    if FAILS:
        print(f"结果: {len(FAILS)} 项失败 -> {FAILS}")
        sys.exit(1)
    print(f"结果: 全部通过([e2e:{RUN_ID}] 测试材料保留在目标环境,可按标记清理)")


if __name__ == "__main__":
    main()

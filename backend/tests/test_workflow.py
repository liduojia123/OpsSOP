"""业务闭环与状态流转测试:故障→解决→案例→审核→SOP→发布→新版本。"""


def _create_fault(client, headers, **overrides):
    payload = {
        "title": "Pod 频繁 OOMKilled",
        "description": "checkout 服务 pod 被 OOMKilled 重启",
        "environment": "prod",
        "service": "checkout",
        "component_version": "1.8.2",
        "error_logs": "Reason: OOMKilled",
        "status": "open",
    }
    payload.update(overrides)
    resp = client.post("/api/faults", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _resolve(client, headers, fault_id):
    resp = client.post(
        f"/api/faults/{fault_id}/resolve",
        json={
            "actual_root_cause": "limits.memory 设置过低",
            "handling_steps": "调整 limits 并重启部署",
            "recovery_verification": "pod 稳定运行 30 分钟无重启",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_full_loop(client, op_headers, rev_headers, adm_headers):
    # 1. 运维创建故障并补充记录
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/records",
        json={"kind": "check_result", "content": "kubectl top 显示内存接近 limits"},
        headers=op_headers,
    )
    assert resp.status_code == 201

    # 2. 人工确认解决 → 生成待审核案例
    detail = _resolve(client, op_headers, fault["id"])
    assert detail["status"] == "resolved"
    assert detail["actual_root_cause"] == "limits.memory 设置过低"
    cases = client.get("/api/cases?status=pending_review", headers=op_headers).json()
    assert len(cases) == 1
    case_id = cases[0]["id"]
    assert cases[0]["confirmed_root_cause"] == "limits.memory 设置过低"

    # 3. 审核通过
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "证据充分"}, headers=rev_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # 4. 从案例生成 SOP 草稿
    resp = client.post(f"/api/cases/{case_id}/generate-sop", headers=op_headers)
    assert resp.status_code == 201, resp.text
    sop = resp.json()
    assert sop["status"] == "draft"
    assert sop["source_case_id"] == case_id

    # 5. 补全并提交 SOP
    resp = client.patch(
        f"/api/sops/{sop['id']}",
        json={
            "title": "Kubernetes Pod OOMKilled 排查与处理",
            "check_steps": "1. kubectl describe pod\n2. 检查 limits",
            "preconditions": "有 kubectl 权限",
            "rollback_steps": "回滚到原 limits",
            "notes": "先在测试环境验证",
        },
        headers=op_headers,
    )
    assert resp.status_code == 200
    resp = client.post(f"/api/sops/{sop['id']}/submit", headers=op_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"

    # 6. 审核发布
    resp = client.post(f"/api/sops/{sop['id']}/approve", json={"comment": "通过"}, headers=rev_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    assert resp.json()["published_at"]

    # 7. 已发布 SOP 对所有登录用户可读
    resp = client.get("/api/sops?status=published", headers=op_headers)
    assert resp.status_code == 200
    assert any(s["id"] == sop["id"] for s in resp.json())

    # 8. 修改已发布 SOP → 新草稿版本,旧版本继续有效
    resp = client.post(f"/api/sops/{sop['id']}/new-version", headers=op_headers)
    assert resp.status_code == 201, resp.text
    v2 = resp.json()
    assert v2["version"] == 2
    assert v2["status"] == "draft"
    assert v2["doc_id"] == sop["doc_id"]

    # v1 仍是 published
    v1 = client.get(f"/api/sops/{sop['id']}", headers=op_headers).json()
    assert v1["status"] == "published"

    # 9. 发布 v2 后 v1 变为历史版本
    client.post(f"/api/sops/{v2['id']}/submit", headers=op_headers)
    resp = client.post(f"/api/sops/{v2['id']}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200
    v1 = client.get(f"/api/sops/{sop['id']}", headers=op_headers).json()
    assert v1["status"] == "superseded"
    assert client.get(f"/api/sops/{v2['id']}", headers=op_headers).json()["status"] == "published"


def test_resolve_requires_confirmation_fields(client, op_headers):
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/resolve",
        json={"actual_root_cause": "", "handling_steps": "", "recovery_verification": ""},
        headers=op_headers,
    )
    assert resp.status_code == 422


def test_case_return_and_resubmit(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]

    # 退回必须有意见
    resp = client.post(f"/api/cases/{case_id}/return", json={"comment": ""}, headers=rev_headers)
    assert resp.status_code == 400

    resp = client.post(
        f"/api/cases/{case_id}/return", json={"comment": "根因描述不完整"}, headers=rev_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "returned"
    assert resp.json()["review_comment"] == "根因描述不完整"

    # 修改后重新提交
    resp = client.patch(
        f"/api/cases/{case_id}",
        json={"confirmed_root_cause": "limits.memory 设置过低(补充了证据)"},
        headers=op_headers,
    )
    assert resp.status_code == 200
    resp = client.post(f"/api/cases/{case_id}/submit", headers=op_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_review"

    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200

    # 已通过不可再改
    resp = client.patch(
        f"/api/cases/{case_id}", json={"confirmed_root_cause": "再改"}, headers=op_headers
    )
    assert resp.status_code == 400


def test_cannot_review_non_pending(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]
    client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "again"}, headers=rev_headers)
    assert resp.status_code == 400
    resp = client.post(f"/api/cases/{case_id}/return", json={"comment": "x"}, headers=rev_headers)
    assert resp.status_code == 400


def test_cannot_edit_resolved_fault(client, op_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    resp = client.patch(f"/api/faults/{fault['id']}", json={"title": "改"}, headers=op_headers)
    assert resp.status_code == 400
    resp = client.post(
        f"/api/faults/{fault['id']}/records",
        json={"kind": "handling_note", "content": "x"},
        headers=op_headers,
    )
    assert resp.status_code == 400


def test_sop_submit_requires_content(client, op_headers):
    resp = client.post(
        "/api/sops", json={"title": "空 SOP"}, headers=op_headers
    )
    sop_id = resp.json()["id"]
    resp = client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)
    assert resp.status_code == 400
    assert "排查步骤" in resp.json()["detail"]


def test_sop_deprecate(client, op_headers, rev_headers):
    resp = client.post(
        "/api/sops",
        json={
            "title": "将废弃的 SOP",
            "check_steps": "s",
            "handling_steps": "h",
            "recovery_verification": "r",
        },
        headers=op_headers,
    )
    sop_id = resp.json()["id"]
    client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)
    client.post(f"/api/sops/{sop_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    resp = client.post(
        f"/api/sops/{sop_id}/deprecate", json={"comment": "过时"}, headers=rev_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deprecated"


def test_sop_feedback(client, op_headers, rev_headers):
    resp = client.post(
        "/api/sops",
        json={
            "title": "反馈测试",
            "check_steps": "s",
            "handling_steps": "h",
            "recovery_verification": "r",
        },
        headers=op_headers,
    )
    sop_id = resp.json()["id"]
    client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)
    client.post(f"/api/sops/{sop_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    resp = client.post(
        f"/api/sops/{sop_id}/feedback",
        json={"kind": "suspected_outdated", "comment": "版本已升级"},
        headers=op_headers,
    )
    assert resp.status_code == 201
    rows = client.get(f"/api/sops/{sop_id}/feedback", headers=op_headers).json()
    assert rows[0]["kind"] == "suspected_outdated"


def test_generate_sop_requires_approved_case(client, op_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]
    resp = client.post(f"/api/cases/{case_id}/generate-sop", headers=op_headers)
    assert resp.status_code == 400  # 未审核通过


def test_workbench_metrics_from_db(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    wb = client.get("/api/workbench", headers=op_headers).json()
    assert any(f["id"] == fault["id"] for f in wb["my_open_faults"])
    assert wb["pending_case_reviews"] == []  # operator 看不到审核队列

    _resolve(client, op_headers, fault["id"])
    wb = client.get("/api/workbench", headers=rev_headers).json()
    assert len(wb["pending_case_reviews"]) == 1


def test_workbench_published_sop_count_not_capped_by_recent_list(client, op_headers, rev_headers):
    """回归(前端缺口):recent_sops 最多 8 条,有效 SOP 总数必须单独统计。"""
    for i in range(10):
        resp = client.post(
            "/api/sops",
            json={
                "title": f"SOP 计数 {i}",
                "check_steps": "s",
                "handling_steps": "h",
                "recovery_verification": "r",
            },
            headers=op_headers,
        )
        sop_id = resp.json()["id"]
        client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)
        resp = client.post(f"/api/sops/{sop_id}/approve", json={"comment": "ok"}, headers=rev_headers)
        assert resp.status_code == 200

    wb = client.get("/api/workbench", headers=op_headers).json()
    assert len(wb["recent_sops"]) == 8  # 展示列表仍限量
    assert wb["published_sop_count"] == 10  # 总数不受列表限量影响


# ===== 回归:质量检查缺陷 =====


def test_create_fault_cannot_bypass_manual_confirm(client, op_headers):
    """回归(质量检查 P1 #2):创建故障不能直接写成已解决。"""
    resp = client.post(
        "/api/faults",
        json={"title": "想直接标记解决", "status": "resolved"},
        headers=op_headers,
    )
    assert resp.status_code == 422, resp.text
    assert "已解决" in resp.text or "resolve" in resp.text

    # draft/open 仍允许
    resp = client.post("/api/faults", json={"title": "正常草稿", "status": "draft"}, headers=op_headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "draft"


def test_resolve_rejects_whitespace_only_fields(client, op_headers):
    """回归(质量检查 P1 #2):人工确认字段拒绝纯空白。"""
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/resolve",
        json={
            "actual_root_cause": "   ",
            "handling_steps": "\t\n",
            "recovery_verification": "  ",
        },
        headers=op_headers,
    )
    assert resp.status_code == 422, resp.text
    # 故障未被误标为已解决
    detail = client.get(f"/api/faults/{fault['id']}", headers=op_headers).json()
    assert detail["status"] == "open"


def test_fault_update_cannot_set_resolved(client, op_headers):
    """回归(质量检查 P1 #2):PATCH 也不能把状态改成 resolved。"""
    fault = _create_fault(client, op_headers)
    resp = client.patch(
        f"/api/faults/{fault['id']}", json={"status": "resolved"}, headers=op_headers
    )
    assert resp.status_code == 400


def test_returned_sop_cannot_publish_without_resubmit(client, op_headers, rev_headers):
    """回归(质量检查 P2 #7):退回 SOP 不能绕过重新提交直接发布。"""
    resp = client.post(
        "/api/sops",
        json={
            "title": "待发布 SOP",
            "check_steps": "排查",
            "handling_steps": "处理",
            "recovery_verification": "验证",
        },
        headers=op_headers,
    )
    sop_id = resp.json()["id"]
    client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)

    # 退回
    resp = client.post(
        f"/api/sops/{sop_id}/return", json={"comment": "需要补充"}, headers=rev_headers
    )
    assert resp.status_code == 200

    # 退回后作者清空处理步骤和恢复验证
    resp = client.patch(
        f"/api/sops/{sop_id}",
        json={"handling_steps": "", "recovery_verification": ""},
        headers=op_headers,
    )
    assert resp.status_code == 200

    # 直接 approve 必须被拒(修复前返回 200/published)
    resp = client.post(f"/api/sops/{sop_id}/approve", json={"comment": "通过"}, headers=rev_headers)
    assert resp.status_code == 400

    # 提交也会因内容不完整被拒
    resp = client.post(f"/api/sops/{sop_id}/submit", headers=op_headers)
    assert resp.status_code == 400

    # 补全后重新提交才能发布
    resp = client.patch(
        f"/api/sops/{sop_id}",
        json={"handling_steps": "补回处理", "recovery_verification": "补回验证"},
        headers=op_headers,
    )
    assert resp.status_code == 200
    assert client.post(f"/api/sops/{sop_id}/submit", headers=op_headers).status_code == 200
    resp = client.post(f"/api/sops/{sop_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"


def test_pending_case_locked_from_silent_edit(client, op_headers, rev_headers):
    """回归(质量检查 P2 #8):待审核案例锁定审核快照,不能无声变更后通过。"""
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]

    # 待审核期间 PATCH 必须被拒:清空走 schema 校验(422),修改走快照锁(400)
    resp = client.patch(
        f"/api/cases/{case_id}", json={"confirmed_root_cause": ""}, headers=op_headers
    )
    assert resp.status_code in (400, 422)
    resp = client.patch(
        f"/api/cases/{case_id}", json={"handling_steps": "悄悄改掉"}, headers=op_headers
    )
    assert resp.status_code == 400

    # 快照未变,审核通过
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200
    assert resp.json()["confirmed_root_cause"] == "limits.memory 设置过低"


def test_case_submit_and_approve_check_all_fields(client, db, op_headers, rev_headers):
    """回归(质量检查 P2 #8):提交与审批校验全部人工确认字段。"""
    from app.models import Case

    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]

    # 退回后修改
    client.post(f"/api/cases/{case_id}/return", json={"comment": "补恢复验证"}, headers=rev_headers)
    resp = client.patch(
        f"/api/cases/{case_id}", json={"recovery_verification": "补充后的验证"}, headers=op_headers
    )
    assert resp.status_code == 200

    # 直接改库把处理步骤清空,模拟历史脏数据:提交必须被拒
    case = db.get(Case, case_id)
    case.handling_steps = "  "
    db.commit()
    resp = client.post(f"/api/cases/{case_id}/submit", headers=op_headers)
    assert resp.status_code == 400
    assert "处理步骤" in resp.json()["detail"]

    # 补全后提交成功
    resp = client.patch(
        f"/api/cases/{case_id}", json={"handling_steps": "补回处理"}, headers=op_headers
    )
    assert resp.status_code == 200
    assert client.post(f"/api/cases/{case_id}/submit", headers=op_headers).status_code == 200

    # 审批侧也复核:直接改库清空根因后 approve 必须被拒
    db.expire_all()
    case = db.get(Case, case_id)
    case.confirmed_root_cause = ""
    db.commit()
    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 400
    assert "根因" in resp.json()["detail"]


def test_case_update_rejects_clearing_confirm_fields(client, op_headers, rev_headers):
    """回归(质量检查 P2 #8):确认字段不能被清空为纯空白。"""
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    case_id = client.get("/api/cases", headers=op_headers).json()[0]["id"]
    client.post(f"/api/cases/{case_id}/return", json={"comment": "改一下"}, headers=rev_headers)

    for field in ("confirmed_root_cause", "handling_steps", "recovery_verification"):
        resp = client.patch(
            f"/api/cases/{case_id}", json={field: "   "}, headers=op_headers
        )
        assert resp.status_code == 422, f"{field} 纯空白应被拒绝: {resp.text}"

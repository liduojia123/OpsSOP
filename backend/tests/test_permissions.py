"""权限测试:角色边界在后端强制执行。"""


def _create_fault(client, headers, title="故障A"):
    resp = client.post(
        "/api/faults",
        json={
            "title": title,
            "description": "服务异常",
            "environment": "prod",
            "service": "checkout",
            "error_logs": "OOMKilled",
            "status": "open",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_operator_cannot_review_case(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    resp = client.post(
        f"/api/faults/{fault['id']}/resolve",
        json={
            "actual_root_cause": "内存泄漏",
            "handling_steps": "扩容并修复",
            "recovery_verification": "监控恢复",
        },
        headers=op_headers,
    )
    assert resp.status_code == 200
    cases = client.get("/api/cases", headers=op_headers).json()
    case_id = cases[0]["id"]

    resp = client.post(f"/api/cases/{case_id}/approve", json={"comment": "ok"}, headers=op_headers)
    assert resp.status_code == 403


def test_operator_cannot_manage_users(client, op_headers):
    resp = client.get("/api/users", headers=op_headers)
    assert resp.status_code == 403
    resp = client.post(
        "/api/users",
        json={
            "username": "x",
            "password": "password123",
            "display_name": "x",
            "role": "operator",
        },
        headers=op_headers,
    )
    assert resp.status_code == 403


def test_operator_cannot_manage_categories(client, op_headers):
    resp = client.post(
        "/api/categories", json={"name": "新分类"}, headers=op_headers
    )
    assert resp.status_code == 403


def test_operator_cannot_see_others_faults(client, db, operator, reviewer, adm_headers):
    from tests.conftest import login

    fault = _create_fault(client, adm_headers, title="管理员的故障")
    # 另一个运维人员
    from app.models import Role, User
    from app.security import hash_password

    other = User(
        username="other_op",
        display_name="other_op",
        password_hash=hash_password("test-password-1"),
        role=Role.operator,
    )
    db.add(other)
    db.commit()
    other_headers = login(client, "other_op")

    resp = client.get("/api/faults", headers=other_headers)
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.get(f"/api/faults/{fault['id']}", headers=other_headers)
    assert resp.status_code == 403


def test_reviewer_cannot_edit_fault(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    resp = client.patch(
        f"/api/faults/{fault['id']}", json={"title": "改标题"}, headers=rev_headers
    )
    assert resp.status_code == 403


def test_draft_sop_hidden_from_other_operators(client, db, op_headers, adm_headers):
    from tests.conftest import login

    resp = client.post(
        "/api/sops",
        json={"title": "内部草稿", "check_steps": "步骤", "handling_steps": "处理", "recovery_verification": "验证"},
        headers=op_headers,
    )
    assert resp.status_code == 201
    sop_id = resp.json()["id"]

    from app.models import Role, User
    from app.security import hash_password

    other = User(
        username="other_op2",
        display_name="other_op2",
        password_hash=hash_password("test-password-1"),
        role=Role.operator,
    )
    db.add(other)
    db.commit()
    other_headers = login(client, "other_op2")

    resp = client.get(f"/api/sops/{sop_id}", headers=other_headers)
    assert resp.status_code == 403
    resp = client.get("/api/sops", headers=other_headers)
    assert all(s["id"] != sop_id for s in resp.json())


def test_reviewer_can_review_but_not_create_fault(client, rev_headers):
    resp = client.post(
        "/api/faults",
        json={"title": "审核人员的故障", "status": "draft"},
        headers=rev_headers,
    )
    assert resp.status_code == 403


def _make_other_operator(db, username: str):
    from app.models import Role, User
    from app.security import hash_password

    other = User(
        username=username,
        display_name=username,
        password_hash=hash_password("test-password-1"),
        role=Role.operator,
    )
    db.add(other)
    db.commit()
    return other


def test_sop_history_hides_private_drafts_across_users(client, db, op_headers):
    """回归(质量检查 P1 #1):跨用户私有草稿不得通过 history 泄露。"""
    from tests.conftest import login

    resp = client.post(
        "/api/sops",
        json={
            "title": "私有草稿内容-敏感",
            "check_steps": "秘密排查步骤",
            "handling_steps": "秘密处理",
            "recovery_verification": "秘密验证",
        },
        headers=op_headers,
    )
    assert resp.status_code == 201
    sop_id = resp.json()["id"]

    _make_other_operator(db, "other_op_hist")
    other_headers = login(client, "other_op_hist")

    # 详情 403
    resp = client.get(f"/api/sops/{sop_id}", headers=other_headers)
    assert resp.status_code == 403
    # history 也必须 403(修复前返回 200 且含草稿全文)
    resp = client.get(f"/api/sops/{sop_id}/history", headers=other_headers)
    assert resp.status_code == 403
    assert "秘密排查步骤" not in resp.text


def test_sop_history_hides_private_new_draft_from_public_old_version(client, db, op_headers, rev_headers):
    """回归(质量检查 P1 #1):通过公开旧版本的 history 不得看到作者未发布的新草稿。"""
    from tests.conftest import login

    # 发布 v1
    resp = client.post(
        "/api/sops",
        json={
            "title": "公开文档",
            "check_steps": "v1 排查",
            "handling_steps": "v1 处理",
            "recovery_verification": "v1 验证",
        },
        headers=op_headers,
    )
    v1_id = resp.json()["id"]
    client.post(f"/api/sops/{v1_id}/submit", headers=op_headers)
    resp = client.post(f"/api/sops/{v1_id}/approve", json={"comment": "ok"}, headers=rev_headers)
    assert resp.status_code == 200

    # 作者创建 v2 草稿(私有内容)
    resp = client.post(f"/api/sops/{v1_id}/new-version", headers=op_headers)
    assert resp.status_code == 201
    v2_id = resp.json()["id"]
    client.patch(
        f"/api/sops/{v2_id}",
        json={"check_steps": "v2 未发布的敏感排查"},
        headers=op_headers,
    )

    _make_other_operator(db, "other_op_hist2")
    other_headers = login(client, "other_op_hist2")

    # 其他运维看 v1 的 history:只应看到 v1,不得出现 v2 草稿
    resp = client.get(f"/api/sops/{v1_id}/history", headers=other_headers)
    assert resp.status_code == 200
    versions = resp.json()
    assert [v["id"] for v in versions] == [v1_id]
    assert "v2 未发布的敏感排查" not in resp.text

    # 也不能直接请求 v2 的 history
    resp = client.get(f"/api/sops/{v2_id}/history", headers=other_headers)
    assert resp.status_code == 403

    # 作者自己可以看到完整历史
    resp = client.get(f"/api/sops/{v1_id}/history", headers=op_headers)
    assert resp.status_code == 200
    assert {v["id"] for v in resp.json()} == {v1_id, v2_id}


def test_user_options_readable_by_all_roles_without_sensitive_fields(
    client, op_headers, rev_headers, adm_headers
):
    """维护人下拉数据源:登录用户可读,且不含口令哈希等敏感字段。"""
    for headers in (op_headers, rev_headers, adm_headers):
        resp = client.get("/api/users/options", headers=headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) >= 1
        for row in rows:
            assert set(row.keys()) == {"id", "username", "display_name", "role"}
        assert "password" not in resp.text.lower()

    # 未登录仍要求鉴权(缺失凭据时 HTTPBearer 返回 401/403,与 test_auth 一致)
    resp = client.get("/api/users/options")
    assert resp.status_code in (401, 403)

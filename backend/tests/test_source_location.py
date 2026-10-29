"""第四阶段第一批:案例来源直达的权限与边界回归。

新增 /cases/:id 入口只复用既有 GET /api/cases/{id} 可见性规则:
- 已通过案例对所有登录用户可读;
- 他人待审/退回案例 403;
- 原始故障、日志、检查记录和生成历史不因案例入口被顺带暴露;
- 检索命中 link 与前端定位路由一致。
"""

from app.models import CaseStatus, Role
from app.search.keyword import search_knowledge
from tests.conftest import login, make_user


def _resolve_case(client, headers, title="来源定位故障"):
    resp = client.post(
        "/api/faults",
        json={
            "title": title,
            "description": "checkout 服务 OOMKilled 重启",
            "environment": "prod",
            "service": "checkout",
            "error_logs": "private-log-line: Reason: OOMKilled",
            "recent_changes": "私有近期变更内容",
            "status": "open",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    fault = resp.json()
    resp = client.post(
        f"/api/faults/{fault['id']}/resolve",
        json={
            "actual_root_cause": "limits.memory 过低",
            "handling_steps": "调整 limits 并重启",
            "recovery_verification": "稳定运行 30 分钟",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    case = client.get("/api/cases", headers=headers).json()[0]
    return fault, case


def test_approved_case_readable_by_other_operator_without_raw_fault(
    client, db, op_headers, rev_headers
):
    fault, case = _resolve_case(client, op_headers)
    assert (
        client.post(
            f"/api/cases/{case['id']}/approve", json={"comment": "通过"}, headers=rev_headers
        ).status_code
        == 200
    )

    other = make_user(db, "other_operator", Role.operator)
    other_headers = login(client, other.username)

    resp = client.get(f"/api/cases/{case['id']}", headers=other_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == CaseStatus.approved.value
    # 案例入口不返回原始故障正文/日志/检查记录/生成历史
    for forbidden in ("error_logs", "recent_changes", "records", "fault"):
        assert forbidden not in data
    assert "private-log-line" not in resp.text
    assert "私有近期变更内容" not in resp.text

    # 原始故障仍按原权限隔离(他人不可读)
    assert client.get(f"/api/faults/{fault['id']}", headers=other_headers).status_code == 403


def test_other_operator_cannot_read_pending_or_returned_case(client, db, op_headers, rev_headers):
    _, case = _resolve_case(client, op_headers, title="待审核来源定位")
    other = make_user(db, "other_operator2", Role.operator)
    other_headers = login(client, other.username)

    assert client.get(f"/api/cases/{case['id']}", headers=other_headers).status_code == 403

    # 退回后仍不对其他运维开放
    assert (
        client.post(
            f"/api/cases/{case['id']}/return", json={"comment": "补充证据"}, headers=rev_headers
        ).status_code
        == 200
    )
    assert client.get(f"/api/cases/{case['id']}", headers=other_headers).status_code == 403


def test_case_detail_errors_are_clear(client, op_headers):
    assert client.get("/api/cases/999999", headers=op_headers).status_code == 404
    # 非整数 ID 由 FastAPI 路径参数校验拒绝,前端显示非法 ID 状态
    assert client.get("/api/cases/not-a-number", headers=op_headers).status_code == 422


def test_search_hit_link_matches_source_route(client, db, op_headers, rev_headers):
    _, case = _resolve_case(client, op_headers, title="检索链接 OOMKILLED")
    client.post(f"/api/cases/{case['id']}/approve", json={"comment": "通过"}, headers=rev_headers)

    from app.models import User

    user = db.query(User).filter(User.username == "op_user").one()
    hits, _ = search_knowledge(db, user, "OOMKILLED")
    case_hits = [h for h in hits if h.source_type == "case"]
    assert case_hits
    assert case_hits[0].link == f"/cases/{case_hits[0].source_id}"
    assert case_hits[0].source_id == case["id"]

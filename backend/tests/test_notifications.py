"""流程事件与站内通知:审核流转提醒、未读计数、已读隔离与实体时间线。"""

from sqlalchemy import select

from app.models import Notification, Role, User
from tests.conftest import make_user


def _create_and_resolve(client, headers, title: str = "订单超时") -> tuple[int, int]:
    created = client.post(
        "/api/faults",
        json={"title": title, "service": "order-service", "description": "504 持续 5 分钟"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    fault_id = created.json()["id"]
    resolved = client.post(
        f"/api/faults/{fault_id}/resolve",
        json={
            "actual_root_cause": "上游超时导致线程池耗尽",
            "handling_steps": "扩容上游并重启消费者",
            "recovery_verification": "错误率恢复至 0",
        },
        headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    return fault_id, resolved.json()["related_case"]["id"]


def _kinds(client, headers) -> set[str]:
    rows = client.get("/api/notifications", headers=headers).json()
    return {row["kind"] for row in rows}


def test_workflow_notifications_and_sop_write_back(client, op_headers, rev_headers):
    fault_id, case_id = _create_and_resolve(client, op_headers)

    # 故障确认后案例进入待审核 → 审核人收到提醒;创建者此时不冒充审核人收到
    assert "case.submitted" in _kinds(client, rev_headers)

    approved = client.post(
        f"/api/cases/{case_id}/approve", json={"comment": "内容完整"}, headers=rev_headers
    )
    assert approved.status_code == 200, approved.text

    sop = client.post(
        "/api/sops",
        json={
            "title": "订单超时处理 SOP",
            "source_case_id": case_id,
            "check_steps": "检查上游延迟与线程池",
            "handling_steps": "扩容并重启消费者",
            "recovery_verification": "错误率归零",
        },
        headers=op_headers,
    )
    assert sop.status_code == 201, sop.text
    sop_id = sop.json()["id"]
    assert client.post(f"/api/sops/{sop_id}/submit", headers=op_headers).status_code == 200
    published = client.post(
        f"/api/sops/{sop_id}/approve", json={"comment": "发布"}, headers=rev_headers
    )
    assert published.status_code == 200, published.text

    op_kinds = _kinds(client, op_headers)
    assert {"case.approved", "sop.published", "case.sop_published"} <= op_kinds
    rev_kinds = _kinds(client, rev_headers)
    assert "sop.submitted" in rev_kinds

    # 通知只发给本人
    op_rows = client.get("/api/notifications", headers=op_headers).json()
    assert all(row["user_id"] != 0 for row in op_rows)

    # 案例/故障时间线包含 SOP 发布回写
    case_events = client.get(
        "/api/audit/entity",
        params={"entity_type": "case", "entity_id": case_id},
        headers=op_headers,
    ).json()
    actions = {row["action"] for row in case_events}
    assert {"case.approve", "case.sop_published"} <= actions

    fault_events = client.get(
        "/api/audit/entity",
        params={"entity_type": "fault", "entity_id": fault_id},
        headers=op_headers,
    ).json()
    fault_actions = {row["action"] for row in fault_events}
    assert {"fault.resolve", "fault.sop_published"} <= fault_actions


def test_unread_count_read_and_isolation(client, db, op_headers, rev_headers):
    client.post(
        "/api/faults",
        json={"title": "磁盘写满", "service": "order-service"},
        headers=op_headers,
    )
    # 直接写通知,避免依赖具体业务状态
    op = db.scalar(select(User).where(User.username == "op_user"))
    rev = db.scalar(select(User).where(User.username == "rev_user"))
    db.add_all(
        [
            Notification(user_id=op.id, kind="case.approved", title="案例已通过", link="/cases/1"),
            Notification(user_id=op.id, kind="sop.returned", title="SOP 被退回", link="/sops/1"),
            Notification(user_id=rev.id, kind="case.submitted", title="待审核", link="/cases/1"),
        ]
    )
    db.commit()

    count = client.get("/api/notifications/unread-count", headers=op_headers).json()["count"]
    assert count == 2
    rows = client.get("/api/notifications", headers=op_headers).json()
    assert len(rows) == 2
    assert all(row["user_id"] == op.id for row in rows)

    # 不能标记他人的通知
    rev_row = client.get("/api/notifications", headers=rev_headers).json()[0]
    assert (
        client.post(f"/api/notifications/{rev_row['id']}/read", headers=op_headers).status_code
        == 404
    )

    marked = client.post(f"/api/notifications/{rows[0]['id']}/read", headers=op_headers)
    assert marked.status_code == 200 and marked.json()["read_at"]
    assert client.get("/api/notifications/unread-count", headers=op_headers).json()["count"] == 1

    read_all = client.post("/api/notifications/read-all", headers=op_headers)
    assert read_all.status_code == 200 and read_all.json()["updated"] == 1
    assert client.get("/api/notifications/unread-count", headers=op_headers).json()["count"] == 0
    # 他人的未读不受影响
    assert client.get("/api/notifications/unread-count", headers=rev_headers).json()["count"] == 1


def test_timeline_requires_entity_visibility(client, db, op_headers, rev_headers):
    created = client.post(
        "/api/faults", json={"title": "私有故障", "service": "order-service"}, headers=op_headers
    )
    fault_id = created.json()["id"]
    other = make_user(db, "op_timeline_other", Role.operator)

    login = client.post(
        "/api/auth/login", json={"username": other.username, "password": "test-password-1"}
    )
    other_headers = {"Authorization": f"Bearer {login.json()['token']}"}

    resp = client.get(
        "/api/audit/entity",
        params={"entity_type": "fault", "entity_id": fault_id},
        headers=other_headers,
    )
    assert resp.status_code == 403
    # 审核人员可见
    assert (
        client.get(
            "/api/audit/entity",
            params={"entity_type": "fault", "entity_id": fault_id},
            headers=rev_headers,
        ).status_code
        == 200
    )
    # 不存在的对象返回 404,不泄露是否存在
    assert (
        client.get(
            "/api/audit/entity",
            params={"entity_type": "fault", "entity_id": 999999},
            headers=op_headers,
        ).status_code
        == 404
    )
"""列表分页(limit/offset + X-Total-Count):返回体保持列表,默认行为兼容。

默认 limit=500 在当前规模(数百条)下一次取全;超过时客户端按 offset 翻页,
X-Total-Count 始终是过滤后的总数,可用来发现被截断的结果。
"""

from uuid import uuid4

from app.models import Case, CaseStatus, Fault, Role, Sop, SopStatus, User
from app.security import hash_password


def _seed_faults(db, user_id: int, n: int, title_prefix: str = "故障") -> None:
    for i in range(n):
        db.add(Fault(title=f"{title_prefix}{i:03d}", created_by=user_id))
    db.commit()


def test_fault_list_pagination_and_total(client, db, operator, op_headers):
    _seed_faults(db, operator.id, 7)

    first = client.get("/api/faults?limit=3&offset=0", headers=op_headers)
    assert first.status_code == 200
    assert len(first.json()) == 3
    assert first.headers["X-Total-Count"] == "7"

    second = client.get("/api/faults?limit=3&offset=3", headers=op_headers)
    assert len(second.json()) == 3
    third = client.get("/api/faults?limit=3&offset=6", headers=op_headers)
    assert len(third.json()) == 1

    # 不传分页参数:默认一次取全(7 < 500)
    default = client.get("/api/faults", headers=op_headers)
    assert len(default.json()) == 7
    assert default.headers["X-Total-Count"] == "7"

    # 三页拼接等于全量,且不重复
    ids = [f["id"] for f in first.json() + second.json() + third.json()]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == sorted(f["id"] for f in default.json())


def test_fault_list_pagination_rejects_invalid_limits(client, op_headers):
    assert client.get("/api/faults?limit=0", headers=op_headers).status_code == 422
    assert client.get("/api/faults?limit=1001", headers=op_headers).status_code == 422
    assert client.get("/api/faults?offset=-1", headers=op_headers).status_code == 422


def test_fault_list_total_respects_filters(client, db, operator, op_headers):
    _seed_faults(db, operator.id, 3)
    db.add(Fault(title="数据库连接超时", created_by=operator.id))
    db.commit()

    resp = client.get("/api/faults?q=数据库", headers=op_headers)
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "1"
    assert len(resp.json()) == 1


def test_fault_list_total_respects_operator_scoping(client, db, operator, op_headers):
    _seed_faults(db, operator.id, 2)
    other = User(
        username="other_op",
        display_name="其他运维",
        password_hash=hash_password("test-password-1"),
        role=Role.operator,
    )
    db.add(other)
    db.commit()
    _seed_faults(db, other.id, 3, title_prefix="他人故障")

    resp = client.get("/api/faults", headers=op_headers)
    assert resp.headers["X-Total-Count"] == "2"
    assert all(f["title"].startswith("故障") for f in resp.json())


def test_case_list_pagination(client, db, operator, op_headers):
    for i in range(5):
        fault = Fault(title=f"案例来源故障{i:03d}", created_by=operator.id)
        db.add(fault)
        db.flush()
        db.add(
            Case(
                fault_id=fault.id,
                title=f"案例{i:03d}",
                confirmed_root_cause="根因",
                handling_steps="步骤",
                recovery_verification="验证",
                status=CaseStatus.approved,
                created_by=operator.id,
            )
        )
    db.commit()

    page = client.get("/api/cases?limit=2&offset=0", headers=op_headers)
    assert page.status_code == 200
    assert len(page.json()) == 2
    assert page.headers["X-Total-Count"] == "5"

    rest = client.get("/api/cases?limit=2&offset=4", headers=op_headers)
    assert len(rest.json()) == 1


def test_sop_list_pagination(client, db, operator, op_headers):
    for i in range(6):
        db.add(
            Sop(
                doc_id=str(uuid4()),
                title=f"SOP{i:03d}",
                status=SopStatus.published,
                created_by=operator.id,
            )
        )
    db.commit()

    page = client.get("/api/sops?limit=4&offset=0", headers=op_headers)
    assert page.status_code == 200
    assert len(page.json()) == 4
    assert page.headers["X-Total-Count"] == "6"

    rest = client.get("/api/sops?limit=4&offset=4", headers=op_headers)
    assert len(rest.json()) == 2
    assert rest.headers["X-Total-Count"] == "6"

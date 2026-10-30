"""统一检索:跨实体结果、关键词匹配、按类型筛选与权限范围。"""

from uuid import uuid4

from app.models import Case, CaseStatus, Fault, FaultStatus, Sop, SopStatus


def _fault(db, user, title, description="", service="", status=FaultStatus.open):
    row = Fault(
        title=title,
        description=description,
        service=service,
        created_by=user.id,
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _case(db, fault, user, title, status=CaseStatus.pending_review, symptom=""):
    row = Case(
        fault_id=fault.id,
        title=title,
        symptom=symptom,
        status=status,
        created_by=user.id,
    )
    db.add(row)
    db.commit()
    return row


def _sop(db, user, title, status=SopStatus.draft, symptom=""):
    row = Sop(
        doc_id=str(uuid4()),
        version=1,
        title=title,
        symptom=symptom,
        status=status,
        created_by=user.id,
        tags=[],
    )
    db.add(row)
    db.commit()
    return row


def _seed(db, operator, reviewer):
    own_fault = _fault(db, operator, "磁盘写满", service="web-01")
    other_fault = _fault(db, reviewer, "磁盘故障(他人)", description="磁盘 inode 耗尽")
    own_case = _case(
        db, own_fault, operator, "磁盘案例(本人)", CaseStatus.approved, "磁盘满导致 502"
    )
    other_approved = _case(
        db, other_fault, reviewer, "磁盘案例(他人通过)", CaseStatus.approved
    )
    # 一个故障只能对应一个案例;待审案例使用另一个不匹配“磁盘”的故障
    other_fault_2 = _fault(db, reviewer, "写入超时")
    other_pending = _case(
        db, other_fault_2, reviewer, "磁盘案例(他人待审)", CaseStatus.pending_review
    )
    other_published = _sop(db, reviewer, "磁盘 SOP(已发布)", SopStatus.published)
    other_draft = _sop(db, reviewer, "磁盘 SOP(他人草稿)", SopStatus.draft)
    own_draft = _sop(db, operator, "磁盘 SOP(本人草稿)", SopStatus.draft)
    return {
        "own_fault": own_fault,
        "other_fault": other_fault,
        "own_case": own_case,
        "other_approved": other_approved,
        "other_pending": other_pending,
        "other_published": other_published,
        "other_draft": other_draft,
        "own_draft": own_draft,
    }


def _group(body, type_name):
    return next(group for group in body["groups"] if group["type"] == type_name)


def test_operator_scope_and_grouping(client, db, operator, reviewer, op_headers):
    rows = _seed(db, operator, reviewer)

    body = client.get("/api/search/all", params={"q": "磁盘"}, headers=op_headers).json()

    faults = _group(body, "fault")
    assert faults["total"] == 1
    assert [item["id"] for item in faults["items"]] == [rows["own_fault"].id]
    assert faults["items"][0]["link"] == f"/faults/{rows['own_fault'].id}"

    cases = _group(body, "case")
    case_ids = {item["id"] for item in cases["items"]}
    assert case_ids == {rows["own_case"].id, rows["other_approved"].id}
    assert rows["other_pending"].id not in case_ids

    sops = _group(body, "sop")
    sop_ids = {item["id"] for item in sops["items"]}
    assert sop_ids == {rows["other_published"].id, rows["own_draft"].id}
    assert rows["other_draft"].id not in sop_ids


def test_reviewer_sees_all(client, db, operator, reviewer, rev_headers):
    _seed(db, operator, reviewer)
    body = client.get("/api/search/all", params={"q": "磁盘"}, headers=rev_headers).json()
    assert _group(body, "fault")["total"] == 2
    assert _group(body, "case")["total"] == 3
    assert _group(body, "sop")["total"] == 3


def test_type_filter_and_field_match(client, db, operator, reviewer, op_headers):
    _fault(db, operator, "写入失败", description="根分区磁盘已满")

    body = client.get(
        "/api/search/all",
        params={"q": "磁盘", "types": "fault"},
        headers=op_headers,
    ).json()
    assert [group["type"] for group in body["groups"]] == ["fault"]
    assert body["groups"][0]["total"] == 1
    assert body["groups"][0]["items"][0]["title"] == "写入失败"


def test_empty_query_and_limit(client, db, operator, reviewer, op_headers):
    rows = _seed(db, operator, reviewer)

    empty = client.get("/api/search/all", params={"q": "  "}, headers=op_headers).json()
    assert [group["total"] for group in empty["groups"]] == [0, 0, 0]
    assert all(group["items"] == [] for group in empty["groups"])

    limited = client.get(
        "/api/search/all", params={"q": "磁盘", "limit": 1}, headers=op_headers
    ).json()
    cases = _group(limited, "case")
    assert cases["total"] == 2  # 本人通过 + 他人通过
    assert len(cases["items"]) == 1
    assert cases["truncated"] is True


def test_search_requires_login(client):
    resp = client.get("/api/search/all", params={"q": "磁盘"})
    assert resp.status_code == 403
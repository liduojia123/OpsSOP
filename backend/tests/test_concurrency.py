"""案例及 SOP 在独立数据库连接并发操作时的冲突保护。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.main import app
from app.models import AuditEvent
from tests.conftest import TestingSessionLocal
from tests.test_workflow import _create_fault, _resolve


def version_headers(auth, item):
    return {**auth, "X-Expected-Updated-At": item["updated_at"]}


def _parallel(client, jobs):
    """每次请求使用独立数据库会话，两个请求从同一快照同时开始。"""
    barrier = Barrier(len(jobs))
    previous = app.dependency_overrides[get_db]

    def independent_db():
        with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = independent_db
    try:
        with TestClient(app) as parallel_client:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                return list(pool.map(lambda j: _send(parallel_client, barrier, j), jobs))
    finally:
        app.dependency_overrides[get_db] = previous


def _send(client, barrier, job):
    barrier.wait(timeout=10)
    return client.request(**job)


def test_stale_sop_edits_and_review_are_rejected(client, op_headers, rev_headers):
    created = client.post("/api/sops", headers=op_headers, json={"title": "SOP", "check_steps": "检查", "handling_steps": "处理", "recovery_verification": "验证"}).json()
    url = f"/api/sops/{created['id']}"
    first = client.patch(url, headers=version_headers(op_headers, created), json={"title": "新标题"})
    assert first.status_code == 200
    stale = client.patch(url, headers=version_headers(op_headers, created), json={"title": "旧标题"})
    assert stale.status_code == 409
    assert client.patch(url, headers={**op_headers, "X-Expected-Updated-At": ""}, json={"title": "x"}).status_code == 428
    assert client.get(url, headers=op_headers).json()["title"] == "新标题"
    assert client.post(url + "/submit", headers=version_headers(op_headers, created)).status_code == 409
    submitted = client.post(url + "/submit", headers=version_headers(op_headers, first.json()))
    assert submitted.status_code == 200
    assert client.post(url + "/approve", headers=version_headers(rev_headers, first.json()), json={"comment": "ok"}).status_code == 409
    assert client.post(url + "/approve", headers=version_headers(rev_headers, submitted.json()), json={"comment": "ok"}).status_code == 200


def test_parallel_sop_edits_do_not_overwrite(client, op_headers):
    item = client.post("/api/sops", headers=op_headers, json={"title": "原始"}).json()
    path = f"/api/sops/{item['id']}"
    headers = version_headers(op_headers, item)
    responses = _parallel(client, [
        {"method": "PATCH", "url": path, "headers": headers, "json": {"title": "甲"}},
        {"method": "PATCH", "url": path, "headers": headers, "json": {"title": "乙"}},
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert client.get(path, headers=op_headers).json()["title"] in ("甲", "乙")


def test_stale_case_edit_submit_and_review(client, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    pending = client.get("/api/cases", headers=op_headers).json()[0]
    path = f"/api/cases/{pending['id']}"
    returned = client.post(path + "/return", headers=version_headers(rev_headers, pending), json={"comment": "补充"}).json()
    edited = client.patch(path, headers=version_headers(op_headers, returned), json={"handling_steps": "新的处理步骤"}).json()
    assert client.patch(path, headers=version_headers(op_headers, returned), json={"handling_steps": "旧步骤"}).status_code == 409
    assert client.post(path + "/submit", headers=version_headers(op_headers, returned)).status_code == 409
    submitted = client.post(path + "/submit", headers=version_headers(op_headers, edited))
    assert submitted.status_code == 200
    assert client.post(path + "/approve", headers=version_headers(rev_headers, pending), json={"comment": "ok"}).status_code == 409
    assert client.get(path, headers=op_headers).json()["handling_steps"] == "新的处理步骤"


def test_parallel_case_approve_and_return_only_one_wins(client, db, op_headers, rev_headers):
    fault = _create_fault(client, op_headers)
    _resolve(client, op_headers, fault["id"])
    item = client.get("/api/cases", headers=op_headers).json()[0]
    headers = version_headers(rev_headers, item)
    path = f"/api/cases/{item['id']}"
    responses = _parallel(client, [
        {"method": "POST", "url": path + "/approve", "headers": headers, "json": {"comment": "通过"}},
        {"method": "POST", "url": path + "/return", "headers": headers, "json": {"comment": "补充证据"}},
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    state = client.get(path, headers=rev_headers).json()["status"]
    assert state in ("approved", "returned")
    events = db.scalars(select(AuditEvent).where(AuditEvent.target_type == "case", AuditEvent.target_id == str(item["id"]), AuditEvent.action.in_(["case.approve", "case.return"]))).all()
    assert len(events) == 1


def test_parallel_new_versions_create_only_one_draft(client, op_headers, rev_headers):
    created = client.post("/api/sops", headers=op_headers, json={"title": "SOP", "check_steps": "检查", "handling_steps": "处理", "recovery_verification": "验证"}).json()
    path = f"/api/sops/{created['id']}"
    submitted = client.post(path + "/submit", headers=version_headers(op_headers, created)).json()
    published = client.post(path + "/approve", headers=version_headers(rev_headers, submitted), json={"comment": "ok"}).json()
    jobs = [{"method": "POST", "url": path + "/new-version", "headers": version_headers(op_headers, published)} for _ in range(2)]
    responses = _parallel(client, jobs)
    assert sorted(r.status_code for r in responses) == [201, 409]
    rows = client.get("/api/sops", headers=op_headers, params={"doc_id": published["doc_id"]}).json()
    assert len([s for s in rows if s["status"] == "draft"]) == 1

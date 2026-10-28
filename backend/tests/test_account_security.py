"""密码重置、登录限流和失败审计。"""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import jwt

from app.config import get_settings
from app.security import ALGORITHM


def test_reset_password_revokes_existing_sessions(client, operator, op_headers, adm_headers):
    assert client.get("/api/auth/me", headers=op_headers).status_code == 200
    response = client.patch(
        f"/api/users/{operator.id}", headers=adm_headers, json={"password": "test-password-1"}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/me", headers=op_headers).status_code == 401
    new_login = client.post("/api/auth/login", json={"username": "op_user", "password": "test-password-1"})
    assert new_login.status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_login.json()['token']}"}).status_code == 200


def test_legacy_session_is_rejected(client, operator):
    token = jwt.encode(
        {"sub": str(operator.id), "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        get_settings().secret_key,
        algorithm=ALGORITHM,
    )
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_login_limit_and_failure_audit(client, operator, adm_headers):
    for _ in range(10):
        response = client.post("/api/auth/login", json={"username": "op_user", "password": "wrong-password"})
        assert response.status_code == 401
    limited = client.post("/api/auth/login", json={"username": "op_user", "password": "test-password-1"})
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    other = client.post("/api/auth/login", json={"username": "unknown_user", "password": "wrong-password"})
    assert other.status_code == 401
    audit = client.get("/api/audit", params={"action": "auth.login_failed"}, headers=adm_headers)
    assert audit.status_code == 200
    assert len(audit.json()) == 11
    assert "wrong-password" not in audit.text
    assert "unknown_user" not in audit.text


def test_rate_window_expires_and_unknown_accounts_are_limited(db):
    from sqlalchemy import update
    from app.models import LoginAttempt
    from app.routers.auth import _LOGIN_WINDOW_SECONDS, _reserve_login

    for _ in range(10):
        assert _reserve_login(db, "not-an-account", "test-source") == 0
    assert _reserve_login(db, "not-an-account", "test-source") > 0
    db.execute(update(LoginAttempt).values(created_at=LoginAttempt.created_at - timedelta(seconds=_LOGIN_WINDOW_SECONDS)))
    db.commit()
    assert _reserve_login(db, "not-an-account", "test-source") == 0


def test_concurrent_attempts_cannot_bypass_account_limit():
    from app.routers.auth import _reserve_login
    from tests.conftest import TestingSessionLocal

    def attempt(_):
        with TestingSessionLocal() as session:
            return _reserve_login(session, "same-user", "test-source")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(30)))
    assert results.count(0) == 10
    assert sum(value > 0 for value in results) == 20


def test_source_limit_applies_across_usernames(db):
    from app.routers.auth import _reserve_login

    for index in range(120):
        assert _reserve_login(db, f"user-{index}", "test-source") == 0
    assert _reserve_login(db, "another-user", "test-source") > 0
    assert _reserve_login(db, "another-user", "another-source") == 0


def test_logout_revokes_only_current_session(client, operator, op_headers):
    another = client.post("/api/auth/login", json={"username": "op_user", "password": "test-password-1"})
    other_headers = {"Authorization": f"Bearer {another.json()['token']}"}
    assert other_headers != op_headers
    assert client.post("/api/auth/logout", headers=op_headers).status_code == 204
    assert client.get("/api/auth/me", headers=op_headers).status_code == 401
    assert client.get("/api/auth/me", headers=other_headers).status_code == 200


def test_change_password_checks_current_and_revokes_all_sessions(client, operator, op_headers):
    another = client.post("/api/auth/login", json={"username": "op_user", "password": "test-password-1"})
    other_headers = {"Authorization": f"Bearer {another.json()['token']}"}
    bad = client.post("/api/auth/change-password", headers=op_headers, json={"current_password": "wrong-password", "new_password": "new-password-1"})
    assert bad.status_code == 400
    assert client.get("/api/auth/me", headers=op_headers).status_code == 200
    same = client.post("/api/auth/change-password", headers=op_headers, json={"current_password": "test-password-1", "new_password": "test-password-1"})
    assert same.status_code == 400
    success = client.post("/api/auth/change-password", headers=op_headers, json={"current_password": "test-password-1", "new_password": "new-password-1"})
    assert success.status_code == 204
    assert client.get("/api/auth/me", headers=op_headers).status_code == 401
    assert client.get("/api/auth/me", headers=other_headers).status_code == 401
    assert client.post("/api/auth/login", json={"username": "op_user", "password": "new-password-1"}).status_code == 200


def test_password_verification_is_rate_limited(client, operator, op_headers, monkeypatch):
    monkeypatch.setattr("app.routers.auth._ACCOUNT_LIMIT", 3)
    payload = {"current_password": "wrong-password", "new_password": "new-password-1"}
    assert client.post("/api/auth/change-password", headers=op_headers, json=payload).status_code == 400
    assert client.post("/api/auth/change-password", headers=op_headers, json=payload).status_code == 400
    assert client.post("/api/auth/change-password", headers=op_headers, json=payload).status_code == 429


def test_audit_cursor_filters_and_permissions(client, db, admin, operator, adm_headers, op_headers):
    from app.models import AuditEvent

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(4):
        db.add(AuditEvent(actor_id=admin.id, action="test.event", target_type="user", target_id=str(index), detail={}, created_at=start + timedelta(days=index)))
    db.add(AuditEvent(actor_id=operator.id, action="test.event", target_type="user", target_id="other", detail={}, created_at=start + timedelta(days=1)))
    db.commit()
    params = {"action": "test.event", "actor_id": admin.id, "start": start.isoformat(), "end": (start + timedelta(days=4)).isoformat(), "limit": 2}
    first = client.get("/api/audit", headers=adm_headers, params=params)
    assert first.status_code == 200
    assert len(first.json()) == 2
    second = client.get("/api/audit", headers=adm_headers, params={**params, "before_id": first.json()[-1]["id"]})
    assert len(second.json()) == 2
    assert {x["id"] for x in first.json()}.isdisjoint({x["id"] for x in second.json()})
    assert all(x["actor_id"] == admin.id for x in first.json() + second.json())
    assert client.get("/api/audit", headers=op_headers, params=params).status_code == 403
    assert client.get("/api/audit", headers=adm_headers, params={"start": "2026-01-01T00:00:00"}).status_code == 400

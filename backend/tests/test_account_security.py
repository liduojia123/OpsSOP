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


def test_rate_window_expires_and_unknown_accounts_are_limited():
    from app.routers.auth import _LOGIN_WINDOW_SECONDS, _login_attempts, _reserve_login

    for _ in range(10):
        assert _reserve_login("not-an-account", "test-source") == 0
    assert _reserve_login("not-an-account", "test-source") > 0
    for bucket in _login_attempts.values():
        bucket[:] = [t - _LOGIN_WINDOW_SECONDS for t in bucket]
    assert _reserve_login("not-an-account", "test-source") == 0


def test_concurrent_attempts_cannot_bypass_account_limit():
    from app.routers.auth import _reserve_login

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: _reserve_login("same-user", "test-source"), range(30)))
    assert results.count(0) == 10
    assert sum(value > 0 for value in results) == 20


def test_source_limit_applies_across_usernames():
    from app.routers.auth import _reserve_login

    for index in range(120):
        assert _reserve_login(f"user-{index}", "test-source") == 0
    assert _reserve_login("another-user", "test-source") > 0
    assert _reserve_login("another-user", "another-source") == 0

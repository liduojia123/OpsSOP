"""认证与会话测试。"""


def test_login_success(client, operator):
    resp = client.post("/api/auth/login", json={"username": "op_user", "password": "test-password-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == "op_user"
    assert body["token"]
    assert body["expires_at"]


def test_login_wrong_password(client, operator):
    resp = client.post("/api/auth/login", json={"username": "op_user", "password": "wrong-pass"})
    assert resp.status_code == 401


def test_me_requires_token(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code in (401, 403)


def test_me_with_token(client, operator, op_headers):
    resp = client.get("/api/auth/me", headers=op_headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"


def test_inactive_user_cannot_login(client, db, admin, adm_headers):
    resp = client.post(
        "/api/users",
        json={
            "username": "disabled_user",
            "password": "test-password-1",
            "display_name": "禁用用户",
            "role": "operator",
        },
        headers=adm_headers,
    )
    assert resp.status_code == 201
    user_id = resp.json()["id"]
    resp = client.patch(f"/api/users/{user_id}", json={"is_active": False}, headers=adm_headers)
    assert resp.status_code == 200
    resp = client.post(
        "/api/auth/login", json={"username": "disabled_user", "password": "test-password-1"}
    )
    assert resp.status_code == 403

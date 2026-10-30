"""个人资料:头像/签名/显示名自更新、格式校验与权限边界。"""

import base64

from sqlalchemy import select

from app.models import AuditEvent, Role, User

# 1x1 PNG(仅用于文件头校验)
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Z4xQAAAAASUVORK5CYII="
)
PNG_DATA_URL = "data:image/png;base64," + PNG_B64


def test_profile_update_and_audit(client, db, op_headers):
    resp = client.patch(
        "/api/auth/profile",
        json={
            "display_name": "值班一号",
            "signature": "先取证，再动手",
            "avatar": PNG_DATA_URL,
        },
        headers=op_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "值班一号"
    assert body["signature"] == "先取证，再动手"
    assert body["avatar"] == PNG_DATA_URL
    assert body["role"] == "operator"

    me = client.get("/api/auth/me", headers=op_headers).json()
    assert me["display_name"] == "值班一号"
    assert me["signature"] == "先取证，再动手"

    events = db.scalars(
        select(AuditEvent).where(AuditEvent.action == "user.update_profile")
    ).all()
    assert events and events[-1].detail["avatar"] == "updated"
    assert events[-1].detail["signature"] == "updated"


def test_profile_clear_avatar_and_signature(client, db, op_headers):
    client.patch(
        "/api/auth/profile",
        json={"signature": "临时签名", "avatar": PNG_DATA_URL},
        headers=op_headers,
    )
    resp = client.patch(
        "/api/auth/profile",
        json={"signature": "", "avatar": ""},
        headers=op_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["signature"] == ""
    assert resp.json()["avatar"] == ""


def test_profile_rejects_invalid_avatar(client, op_headers):
    bad_values = [
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "data:image/png;base64,not-base64!!",
        "data:image/png;base64," + base64.b64encode(b"plain text").decode(),
        "https://example.com/avatar.png",
    ]
    for value in bad_values:
        resp = client.patch(
            "/api/auth/profile", json={"avatar": value}, headers=op_headers
        )
        assert resp.status_code == 400, (value[:30], resp.text)


def test_profile_rejects_oversized_avatar(client, op_headers):
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (256 * 1024)).decode()
    resp = client.patch(
        "/api/auth/profile",
        json={"avatar": "data:image/png;base64," + payload},
        headers=op_headers,
    )
    assert resp.status_code == 400


def test_profile_cannot_escalate_role_or_disable_self(client, db, op_headers):
    resp = client.patch(
        "/api/auth/profile",
        json={"display_name": "仍是运维", "role": "admin", "is_active": False},
        headers=op_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"
    assert resp.json()["is_active"] is True
    user = db.scalar(select(User).where(User.username == "op_user"))
    assert user.role == Role.operator and user.is_active is True


def test_profile_validation_and_auth(client, op_headers):
    assert client.patch("/api/auth/profile", json={"signature": "x"}).status_code == 403
    assert (
        client.patch(
            "/api/auth/profile", json={"display_name": "   "}, headers=op_headers
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/auth/profile", json={"signature": "x" * 201}, headers=op_headers
        ).status_code
        == 422
    )


def test_admin_user_list_includes_profile(client, adm_headers, op_headers):
    client.patch(
        "/api/auth/profile",
        json={"signature": "我为团队守夜", "avatar": PNG_DATA_URL},
        headers=op_headers,
    )
    users = client.get("/api/users", headers=adm_headers).json()
    operator = next(row for row in users if row["username"] == "op_user")
    assert operator["signature"] == "我为团队守夜"
    assert operator["avatar"] == PNG_DATA_URL
    # 普通用户不能读取账号列表
    assert client.get("/api/users", headers=op_headers).status_code == 403
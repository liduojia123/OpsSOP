"""账号管理与审计权限。"""


def test_admin_updates_user_and_records_changes(client, admin, operator, adm_headers):
    response = client.patch(
        f"/api/users/{operator.id}",
        headers=adm_headers,
        json={"display_name": "  新名称  ", "role": "reviewer"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "新名称"
    assert response.json()["role"] == "reviewer"

    events = client.get("/api/audit", params={"action": "user.update"}, headers=adm_headers)
    assert events.status_code == 200
    event = events.json()[0]
    assert event["actor_id"] == admin.id
    assert event["target_id"] == str(operator.id)
    assert event["detail"]["role"] == {"from": "operator", "to": "reviewer"}
    assert event["detail"]["display_name"] == {"from": "op_user", "to": "新名称"}


def test_admin_cannot_remove_own_role_or_deactivate(client, admin, adm_headers):
    for changes in ({"role": "operator"}, {"is_active": False}):
        response = client.patch(f"/api/users/{admin.id}", headers=adm_headers, json=changes)
        assert response.status_code == 400
    assert client.get("/api/auth/me", headers=adm_headers).json()["role"] == "admin"


def test_operator_cannot_update_users_or_read_audit(client, operator, op_headers):
    response = client.patch(f"/api/users/{operator.id}", headers=op_headers, json={"role": "admin"})
    assert response.status_code == 403
    assert client.get("/api/audit", headers=op_headers).status_code == 403


def test_blank_display_name_is_rejected(client, operator, adm_headers):
    response = client.patch(f"/api/users/{operator.id}", headers=adm_headers, json={"display_name": "   "})
    assert response.status_code == 422

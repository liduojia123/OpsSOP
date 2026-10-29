"""备份管理接口的权限与空列表行为。"""

from app.config import Settings


def test_backup_endpoints_require_admin(client, op_headers, adm_headers, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.routers.backups.get_settings",
        lambda: Settings(backup_dir=str(tmp_path), backup_enabled=True),
    )

    assert client.get("/api/admin/backups", headers=op_headers).status_code == 403
    assert client.get("/api/admin/backups", headers=op_headers).status_code != 200

    listed = client.get("/api/admin/backups", headers=adm_headers)
    assert listed.status_code == 200
    assert listed.json() == []

    config = client.get("/api/admin/backups/config", headers=adm_headers)
    assert config.status_code == 200
    assert config.json()["directory"] == str(tmp_path)
    assert config.json()["retention_days"] == 30


def test_download_rejects_unknown_or_traversal_names(client, adm_headers):
    assert client.get("/api/admin/backups/opssop-20261028T000000Z", headers=adm_headers).status_code == 404
    assert client.get("/api/admin/backups/..%2F..%2Fetc%2Fpasswd", headers=adm_headers).status_code == 404

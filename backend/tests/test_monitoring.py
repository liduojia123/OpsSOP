"""监控与 SSH 只读诊断:URL/PromQL 校验、模板注入防护、指纹校验、接口权限与审计。"""

import json
import subprocess

import pytest
from sqlalchemy import select

from app.ai.keys import encrypt_secret
from app.config import get_settings
from app.models import MonitoredServer, MonitoringConfig, SshCommandTemplate
from app.monitoring.prometheus import (
    MonitoringError,
    PrometheusClient,
    QueryResult,
    Series,
    validate_prometheus_url,
    validate_query,
)
from app.monitoring.ssh import SshError, SshTarget, _FingerprintPolicy, fingerprint_of, run_command
from app.monitoring.templates import TemplateError, render_command


# ===== Prometheus 校验 =====

def test_validate_url_accepts_lan_http_and_https():
    assert validate_prometheus_url("http://192.168.196.30:9090") == "http://192.168.196.30:9090"
    assert validate_prometheus_url("https://prom.example.com/") == "https://prom.example.com"


def test_validate_url_rejects_bad_inputs():
    for value in ("", "ftp://host", "http://user:pass@host:9090", "http://host:9090?a=1", "not a url"):
        with pytest.raises(MonitoringError):
            validate_prometheus_url(value)


def test_validate_query_rejects_empty_newline_and_long():
    with pytest.raises(MonitoringError):
        validate_query("")
    with pytest.raises(MonitoringError):
        validate_query("up\nnode_load1")
    with pytest.raises(MonitoringError):
        validate_query("x" * 601)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content if content is not None else json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class _FakeClient:
    response = _FakeResponse()
    captured: dict = {}

    def __init__(self, **kwargs):
        _FakeClient.captured = dict(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        _FakeClient.captured["url"] = url
        _FakeClient.captured["params"] = params
        return _FakeClient.response


def test_prometheus_query_parses_vector(monkeypatch):
    _FakeClient.response = _FakeResponse(
        payload={"status": "success", "data": {"result": [{"metric": {"instance": "a"}, "value": [1, "2.5"]}]}}
    )
    monkeypatch.setattr("app.monitoring.prometheus.httpx.Client", _FakeClient)
    result = PrometheusClient("http://prom:9090").query("up")
    assert result.series[0].labels["instance"] == "a"
    assert result.series[0].points[0][1] == 2.5
    assert _FakeClient.captured["params"] == {"query": "up"}


def test_prometheus_range_caps_points_and_series(monkeypatch):
    values = [[i, str(i)] for i in range(800)]
    _FakeClient.response = _FakeResponse(
        payload={
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"instance": f"i{n}"}, "values": values}
                    for n in range(60)
                ]
            },
        }
    )
    monkeypatch.setattr("app.monitoring.prometheus.httpx.Client", _FakeClient)
    result = PrometheusClient("http://prom:9090").query_range("up", minutes=60, step=60)
    assert len(result.series) == 50
    assert all(len(s.points) <= 720 for s in result.series)
    assert result.truncated is True
    # Prometheus 只接受时间戳;此前传 "now-60m" 会被 400 拒绝
    params = _FakeClient.captured["params"]
    assert float(params["start"]) < float(params["end"])
    assert params["step"] == "60"


def test_prometheus_maps_errors(monkeypatch):
    monkeypatch.setattr("app.monitoring.prometheus.httpx.Client", _FakeClient)
    for status, code in ((401, "auth"), (500, "upstream"), (400, "bad_query")):
        _FakeClient.response = _FakeResponse(status_code=status, payload={})
        with pytest.raises(MonitoringError) as exc:
            PrometheusClient("http://prom:9090").query("up")
        assert exc.value.code == code


def test_prometheus_rejects_oversized_response(monkeypatch):
    _FakeClient.response = _FakeResponse(content=b"x" * (2 * 1024 * 1024 + 1))
    monkeypatch.setattr("app.monitoring.prometheus.httpx.Client", _FakeClient)
    with pytest.raises(MonitoringError) as exc:
        PrometheusClient("http://prom:9090").query("up")
    assert exc.value.code == "too_large"


# ===== 模板渲染 =====

def test_render_static_template():
    assert render_command("uptime", {}, {}) == "uptime"


def test_render_service_template_quotes_unit():
    rendered = render_command("systemctl status $unit --no-pager", {"unit": {"required": True}}, {"unit": "nginx"})
    assert rendered == "systemctl status nginx --no-pager"


def test_render_rejects_injection_and_bad_args():
    schema = {"unit": {"required": True, "pattern": r"^[A-Za-z0-9@._:-]{1,64}$"}}
    for args in ({"unit": "nginx; rm -rf /"}, {"unit": "a b"}, {}, {"unit": "nginx", "extra": "x"}):
        with pytest.raises(TemplateError):
            render_command("systemctl status $unit", schema, args)
    with pytest.raises(TemplateError):
        render_command("journalctl -n $lines", {"lines": {"type": "int", "min": 20, "max": 200}}, {"lines": 5})


def test_render_rejects_unsubstituted_placeholder():
    with pytest.raises(TemplateError):
        render_command("echo $missing", {}, {})


# ===== SSH 指纹与输出 =====

class _FakeKey:
    def __init__(self, raw=b"key"):
        self.raw = raw

    def asbytes(self):
        return self.raw


def test_fingerprint_policy_rejects_mismatch_and_missing():
    key = _FakeKey()
    with pytest.raises(SshError) as exc:
        _FingerprintPolicy("").missing_host_key(None, "host", key)
    assert exc.value.code == "host_key_missing"
    with pytest.raises(SshError) as exc:
        _FingerprintPolicy("SHA256:wrong").missing_host_key(None, "host", key)
    assert exc.value.code == "host_key_mismatch"
    _FingerprintPolicy(fingerprint_of(key)).missing_host_key(None, "host", key)


class _FakeChannel:
    def __init__(self, exit_status=0):
        self.exit_status = exit_status
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def recv_exit_status(self):
        return self.exit_status


class _FakeStream:
    def __init__(self, data: bytes, channel):
        self.data = data
        self.channel = _FakeChannel()

    def read(self, size):
        return self.data[:size]


class _FakeSSH:
    def __init__(self, out=b"", err=b"", exit_status=0):
        self.out = out
        self.err = err
        self.exit_status = exit_status
        self.command = None

    def exec_command(self, command, timeout=None):
        self.command = command
        return None, _FakeStream(self.out, None), _FakeStream(self.err, None)

    def close(self):
        pass


def test_run_command_truncates_and_reports_exit(monkeypatch):
    fake = _FakeSSH(out=b"x" * 5000, err=b"", exit_status=0)

    class Stream(_FakeStream):
        def __init__(self, data):
            super().__init__(data, None)
            self.channel = _FakeChannel(0)

    fake.exec_command = lambda command, timeout=None: (None, Stream(fake.out), Stream(fake.err))
    monkeypatch.setattr("app.monitoring.ssh._connect", lambda target, policy, timeout: fake)
    result = run_command(SshTarget("h", 22, "u", "pem", "SHA256:x"), "uptime")
    assert result["exit_status"] == 0
    assert result["truncated"] is True
    assert len(result["stdout"]) == 2000


# ===== 接口 =====

def test_monitoring_admin_requires_admin(client, op_headers):
    assert client.get("/api/admin/monitoring", headers=op_headers).status_code == 403
    assert client.put(
        "/api/admin/monitoring",
        json={"enabled": False, "prometheus_url": ""},
        headers=op_headers,
    ).status_code == 403


def test_config_save_and_seed(client, db, adm_headers):
    resp = client.put(
        "/api/admin/monitoring",
        json={"enabled": True, "prometheus_url": "http://192.168.196.30:9090", "timeout_seconds": 8, "ai_tools_enabled": True},
        headers=adm_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    row = db.get(MonitoringConfig, 1)
    assert row.prometheus_url.endswith(":9090")

    templates = client.get("/api/admin/ssh-templates", headers=adm_headers)
    assert templates.status_code == 200
    keys = [t["key"] for t in templates.json()]
    assert "uptime" in keys and "journal" in keys
    assert db.scalars(select(SshCommandTemplate).where(SshCommandTemplate.key == "uptime")).first()

    panels = client.get("/api/admin/dashboard-panels", headers=adm_headers)
    assert panels.status_code == 200
    assert len(panels.json()) >= 3


def test_config_rejects_invalid_url(client, adm_headers):
    resp = client.put(
        "/api/admin/monitoring",
        json={"enabled": True, "prometheus_url": "ftp://bad"},
        headers=adm_headers,
    )
    assert resp.status_code == 400


def test_server_crud_encrypts_key(client, db, adm_headers):
    created = client.post(
        "/api/admin/servers",
        json={
            "name": "ubuntu3",
            "host": "192.168.196.30",
            "port": 22,
            "username": "ubuntu1",
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
            "prometheus_instance": "192.168.196.30:9100",
        },
        headers=adm_headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["has_key"] is True
    assert "secret" not in json.dumps(body, ensure_ascii=False)
    server_id = body["id"]

    row = db.get(MonitoredServer, server_id)
    assert "secret" not in row.private_key_encrypted

    updated = client.patch(
        f"/api/admin/servers/{server_id}",
        json={
            "name": "ubuntu3-renamed",
            "host": "192.168.196.30",
            "port": 22,
            "username": "ubuntu1",
            "private_key": "",
            "host_key_fingerprint": "SHA256:abc",
        },
        headers=adm_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "ubuntu3-renamed"
    assert updated.json()["host_key_fingerprint"] == "SHA256:abc"

    deleted = client.delete(f"/api/admin/servers/{server_id}", headers=adm_headers)
    assert deleted.status_code == 204


def test_server_create_requires_key(client, adm_headers):
    resp = client.post(
        "/api/admin/servers",
        json={"name": "x", "host": "10.0.0.5", "username": "u", "private_key": ""},
        headers=adm_headers,
    )
    assert resp.status_code == 400


def test_server_test_returns_fingerprint(client, db, adm_headers, monkeypatch):
    server = MonitoredServer(
        name="s",
        host="10.0.0.5",
        port=22,
        username="u",
        private_key_encrypted=encrypt_secret("pem", get_settings().secret_key),
        host_key_fingerprint="",
    )
    db.add(server)
    db.commit()
    monkeypatch.setattr("app.routers.monitoring_admin.probe_fingerprint", lambda target: "SHA256:probed")
    resp = client.post(f"/api/admin/servers/{server.id}/test", json={}, headers=adm_headers)
    assert resp.status_code == 200
    assert resp.json()["fingerprint"] == "SHA256:probed"


def test_overview_returns_panels(client, db, adm_headers, monkeypatch):
    client.put(
        "/api/admin/monitoring",
        json={"enabled": True, "prometheus_url": "http://192.168.196.30:9090"},
        headers=adm_headers,
    )

    class FakeProm:
        def query_range(self, expr, minutes=60, step=60):
            return QueryResult(series=[Series(labels={"instance": "a"}, points=[(1, 2.0)])])

        def query(self, expr):
            return QueryResult(series=[Series(labels={"instance": "a"}, points=[(1, 2.0)])])

        def targets(self):
            return [{"job": "node", "instance": "a", "health": "up"}]

    monkeypatch.setattr("app.monitoring.service.resolve_client", lambda db: FakeProm())
    resp = client.get("/api/monitoring/overview", headers=adm_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["panels"] and body["panels"][0]["series"]
    assert body["targets"][0]["health"] == "up"


def test_manual_run_requires_operator_and_audits(client, db, rev_headers, op_headers, adm_headers, monkeypatch):
    server = MonitoredServer(
        name="s1",
        host="10.0.0.6",
        port=22,
        username="u",
        private_key_encrypted=encrypt_secret("pem", get_settings().secret_key),
        host_key_fingerprint="SHA256:x",
    )
    db.add(server)
    db.commit()

    assert client.post(
        f"/api/monitoring/servers/{server.id}/run",
        json={"template": "uptime"},
        headers=rev_headers,
    ).status_code == 403

    monkeypatch.setattr(
        "app.routers.monitoring.execute_server_template",
        lambda db, server, template, args, settings: {
            "server": server.name,
            "template": template,
            "command": "uptime",
            "stdout": "up 1 day",
            "exit_status": 0,
            "truncated": False,
            "latency_ms": 12,
        },
    )
    resp = client.post(
        f"/api/monitoring/servers/{server.id}/run",
        json={"template": "uptime"},
        headers=op_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["stdout"] == "up 1 day"

    events = client.get("/api/audit", params={"action": "monitoring.ssh.run"}, headers=adm_headers).json()
    assert events and events[0]["detail"]["status"] == "ok"
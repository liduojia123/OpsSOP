"""用户自填模型厂商配置:加密、厂商注册表、模型列表拉取、接口权限与生效链路。"""

import json

import pytest

from app.ai import keys as keys_mod
from app.ai import providers as providers_mod
from app.ai.keys import (
    KeyError_,
    decrypt_secret,
    delete_user_key,
    effective_settings,
    encrypt_secret,
    list_user_keys,
    resolve_user_config,
    save_user_key,
)
from app.ai.llm import LLMError, OpenAICompatEndpoint, build_adapter
from app.ai.providers import (
    PROVIDERS,
    ProviderError,
    UserAiConfig,
    fetch_models,
    get_provider,
    parse_models_payload,
)
from app.config import Settings
from app.models import Role, User, UserAiKey


SECRET = "unit-test-secret-key-0123456789"


def _settings(**overrides) -> Settings:
    values = {"secret_key": SECRET, "ai_mode": "disabled", "local_only": False}
    values.update(overrides)
    return Settings(**values)


# ===== 加密 =====

def test_encrypt_decrypt_roundtrip():
    token = encrypt_secret("sk-abc123", SECRET)
    assert "sk-abc123" not in token
    assert decrypt_secret(token, SECRET) == "sk-abc123"


def test_decrypt_rejects_tampered_ciphertext():
    token = encrypt_secret("sk-abc123", SECRET)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(KeyError_):
        decrypt_secret(tampered, SECRET)


def test_decrypt_rejects_wrong_server_key():
    token = encrypt_secret("sk-abc123", SECRET)
    with pytest.raises(KeyError_):
        decrypt_secret(token, "another-secret-key-0123456789")


def test_encrypt_rejects_empty():
    with pytest.raises(KeyError_):
        encrypt_secret("", SECRET)


# ===== 厂商注册表 =====

def test_registry_contains_three_providers_with_doc_verified_values():
    assert set(PROVIDERS) == {"deepseek", "minimax", "mimo"}
    deepseek = get_provider("deepseek")
    assert deepseek.base_url == "https://api.deepseek.com"
    assert deepseek.token_field == "max_tokens"
    assert "deepseek-flash" in deepseek.default_models
    minimax = get_provider("minimax")
    assert minimax.token_field == "max_completion_tokens"
    assert minimax.base_url.endswith("/v1")
    mimo = get_provider("mimo")
    assert mimo.base_url == "https://api.xiaomimimo.com/v1"
    assert "mimo-v2.6-pro" in mimo.default_models


def test_minimax_flash_requires_thinking_adaptive():
    spec = get_provider("minimax")
    assert spec.extra_body("MiniMax-M3.1-Flash") == {"thinking": {"type": "adaptive"}}
    assert spec.extra_body("MiniMax-M3") == {}


def test_unknown_provider_rejected():
    with pytest.raises(ProviderError):
        get_provider("not-a-provider")


def test_normalize_base_url_requires_https():
    spec = get_provider("deepseek")
    with pytest.raises(ProviderError):
        providers_mod.normalize_base_url(spec, "http://insecure.example.com/v1")
    assert providers_mod.normalize_base_url(spec, "") == spec.base_url


# ===== 模型列表解析 =====

def test_parse_models_payload_reads_openai_shape():
    models = parse_models_payload(
        {"data": [{"id": "model-b", "object": "model"}, {"id": "model-a"}]}
    )
    assert [m["id"] for m in models] == ["model-a", "model-b"]


def test_parse_models_payload_reads_deepseek_extra_fields():
    models = parse_models_payload(
        {
            "data": [
                {
                    "id": "deepseek-v4-pro",
                    "name": "DeepSeek-V4-Pro",
                    "context_window": 1000000,
                    "max_output_tokens": 384000,
                    "effort": {"supported_levels": ["low", "high"], "default_level": "low"},
                }
            ]
        }
    )
    assert models[0]["context_window"] == 1000000
    assert models[0]["reasoning_levels"] == ["low", "high"]


def test_parse_models_payload_rejects_bad_shape():
    with pytest.raises(ProviderError):
        parse_models_payload({"data": "not-a-list"})
    with pytest.raises(ProviderError):
        parse_models_payload({"data": []})


def test_fetch_models_uses_bearer_and_openai_path(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse(200, {"data": [{"id": "m1"}]})

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    monkeypatch.setattr(providers_mod.httpx, "Client", FakeClient)
    models = fetch_models("https://api.example.com/v1", "sk-key")
    assert models == [{"id": "m1"}]
    assert captured["url"] == "https://api.example.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-key"


def test_fetch_models_auth_failure_is_specific(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            class R:
                status_code = 401

            return R()

    monkeypatch.setattr(providers_mod.httpx, "Client", FakeClient)
    with pytest.raises(ProviderError) as exc:
        fetch_models("https://api.example.com/v1", "bad-key")
    assert exc.value.code == "auth"


# ===== 保存与生效 =====

def test_save_user_key_encrypts_and_resolves(db, operator):
    settings = _settings()
    save_user_key(db, operator, "deepseek", "sk-user-key", settings, model="deepseek-flash")
    db.commit()

    rows = list_user_keys(db, operator.id)
    assert len(rows) == 1
    assert rows[0].provider == "deepseek"
    assert "sk-user-key" not in rows[0].api_key_encrypted

    config = resolve_user_config(db, operator, settings)
    assert config is not None
    assert config.provider == "deepseek"
    assert config.model == "deepseek-flash"
    assert config.api_key == "sk-user-key"


def test_save_user_key_rejected_in_local_only(db, operator):
    settings = _settings(local_only=True)
    with pytest.raises(KeyError_):
        save_user_key(db, operator, "deepseek", "sk-key", settings)
    assert list_user_keys(db, operator.id) == []


def test_resolve_user_config_none_when_no_key(db, operator):
    assert resolve_user_config(db, operator, _settings()) is None


def test_resolve_user_config_none_in_local_only(db, operator):
    settings = _settings()
    save_user_key(db, operator, "deepseek", "sk-key", settings, model="deepseek-flash")
    db.commit()
    assert resolve_user_config(db, operator, _settings(local_only=True)) is None


def test_delete_user_key(db, operator):
    settings = _settings()
    save_user_key(db, operator, "mimo", "sk-key", settings, model="mimo-v2.6-pro")
    db.commit()
    assert delete_user_key(db, operator.id, "mimo") is True
    db.commit()
    assert list_user_keys(db, operator.id) == []


def test_effective_settings_switches_to_cloud_and_changes_version(db, operator):
    settings = _settings()
    save_user_key(db, operator, "minimax", "sk-key", settings, model="MiniMax-M3")
    db.commit()
    config = resolve_user_config(db, operator, settings)
    updated = effective_settings(settings, config)
    assert updated.ai_mode == "cloud"
    assert updated.ai_base_url == get_provider("minimax").base_url
    assert updated.ai_model == "MiniMax-M3"
    # 配置版本随厂商/模型变化:缓存不跨厂商复用
    assert updated.ai_config_version != settings.ai_config_version


# ===== 适配层 =====

def test_adapter_uses_provider_token_field_and_extra_body():
    config = UserAiConfig(
        provider="minimax",
        base_url="https://api.minimaxi.com/v1",
        model="MiniMax-M3.1-Flash",
        api_key="sk-key",
    )
    adapter = build_adapter(_settings(), user_config=config)
    endpoint: OpenAICompatEndpoint = adapter.endpoint
    assert endpoint.token_field == "max_completion_tokens"
    assert endpoint.extra_body == {"thinking": {"type": "adaptive"}}
    assert adapter.describe()["has_api_key"] is True


def test_adapter_rejects_cloud_when_local_only():
    config = UserAiConfig(
        provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="k"
    )
    with pytest.raises(LLMError):
        build_adapter(_settings(local_only=True), user_config=config)


# ===== 接口 =====

def test_providers_endpoint_lists_providers_without_keys(client, adm_headers):
    resp = client.get("/api/ai/providers", headers=adm_headers)
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert ids == ["deepseek", "minimax", "mimo"]
    for item in resp.json():
        assert "api_key" not in item
        assert item["has_key"] is False


def test_save_and_read_key_never_returns_secret(client, db, adm_headers):
    resp = client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-super-secret-value", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_key"] is True
    assert "sk-super-secret-value" not in json.dumps(body)

    listed = client.get("/api/ai/keys", headers=adm_headers)
    assert listed.status_code == 200
    assert "sk-super-secret-value" not in listed.text

    providers = client.get("/api/ai/providers", headers=adm_headers)
    assert "sk-super-secret-value" not in providers.text

    # 落库的是密文
    row = db.query(UserAiKey).filter(UserAiKey.provider == "deepseek").one()
    assert "sk-super-secret-value" not in row.api_key_encrypted
    assert row.api_key_encrypted


def test_delete_key_removes_configuration(client, adm_headers):
    client.put(
        "/api/ai/providers/mimo/key",
        json={"api_key": "sk-x", "model": "mimo-v2.6-pro"},
        headers=adm_headers,
    )
    resp = client.delete("/api/ai/providers/mimo/key", headers=adm_headers)
    assert resp.status_code == 204
    assert client.get("/api/ai/keys", headers=adm_headers).json() == []


def test_models_endpoint_requires_key(client, adm_headers):
    resp = client.post("/api/ai/providers/deepseek/models", json={}, headers=adm_headers)
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_models_endpoint_falls_back_when_provider_has_no_list(client, adm_headers, monkeypatch):
    from app.routers import ai_settings

    def boom(*a, **k):
        raise ProviderError("not_found", "该厂商未提供模型列表接口")

    monkeypatch.setattr(ai_settings, "fetch_models", boom)
    resp = client.post(
        "/api/ai/providers/deepseek/models",
        json={"api_key": "sk-key"},
        headers=adm_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert "deepseek-flash" in [m["id"] for m in body["models"]]


def test_models_endpoint_returns_provider_models(client, adm_headers, monkeypatch):
    from app.routers import ai_settings

    monkeypatch.setattr(
        ai_settings, "fetch_models", lambda *a, **k: [{"id": "m1"}, {"id": "m2"}]
    )
    resp = client.post(
        "/api/ai/providers/minimax/models",
        json={"api_key": "sk-key"},
        headers=adm_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "provider"
    assert [m["id"] for m in resp.json()["models"]] == ["m1", "m2"]


def test_ai_status_reports_user_provider(client, adm_headers):
    client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-key", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    resp = client.get("/api/system/ai-status", headers=adm_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_ai_enabled"] is True
    assert body["user_provider"] == "deepseek"
    assert "DeepSeek" in body["message"]


# ===== AI 总开关 =====

def test_toggle_requires_saved_key(client, adm_headers):
    resp = client.put("/api/ai/enabled", json={"enabled": True}, headers=adm_headers)
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_toggle_off_keeps_key_but_disables_use(client, db, adm_headers):
    client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-keep-me", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    off = client.put("/api/ai/enabled", json={"enabled": False}, headers=adm_headers)
    assert off.status_code == 200
    assert off.json() == {"enabled": False, "has_key": True}

    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_ai_configured"] is True
    assert status["user_ai_enabled"] is False
    assert "已关闭" in status["message"]

    # Key 仍在(只是禁用),重新开启无需重填
    listed = client.get("/api/ai/keys", headers=adm_headers).json()
    assert listed[0]["has_key"] is True
    assert listed[0]["enabled"] is False

    on = client.put("/api/ai/enabled", json={"enabled": True}, headers=adm_headers)
    assert on.status_code == 200
    assert on.json()["enabled"] is True
    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_ai_enabled"] is True


def test_disabled_user_config_is_not_used(db, operator):
    settings = _settings()
    save_user_key(db, operator, "deepseek", "sk-key", settings, model="deepseek-flash")
    db.commit()
    assert resolve_user_config(db, operator, settings) is not None

    from app.ai.keys import set_user_ai_enabled

    set_user_ai_enabled(db, operator.id, False)
    db.commit()
    assert resolve_user_config(db, operator, settings) is None


# ===== 切换生效厂商(对话页下拉) =====

def test_activate_key_switches_effective_provider(client, adm_headers):
    client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-a", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    client.put(
        "/api/ai/providers/minimax/key",
        json={"api_key": "sk-b", "model": "MiniMax-M3"},
        headers=adm_headers,
    )
    # 保存后两行都启用:生效的是字典序第一条,这正是下拉切换要消除的含糊状态
    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_provider"] == "deepseek"

    resp = client.put("/api/ai/keys/minimax/active", headers=adm_headers)
    assert resp.status_code == 200
    flags = {item["provider"]: item["enabled"] for item in resp.json()}
    assert flags == {"deepseek": False, "minimax": True}

    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_provider"] == "minimax"
    assert status["user_ai_enabled"] is True


def test_activate_key_requires_saved_key(client, adm_headers):
    resp = client.put("/api/ai/keys/mimo/active", headers=adm_headers)
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_activate_unknown_provider_rejected(client, adm_headers):
    resp = client.put("/api/ai/keys/not-a-provider/active", headers=adm_headers)
    assert resp.status_code == 400


def test_activate_reenables_ai_after_toggle_off(client, adm_headers):
    client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-a", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    off = client.put("/api/ai/enabled", json={"enabled": False}, headers=adm_headers)
    assert off.status_code == 200
    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_ai_enabled"] is False

    resp = client.put("/api/ai/keys/deepseek/active", headers=adm_headers)
    assert resp.status_code == 200
    status = client.get("/api/system/ai-status", headers=adm_headers).json()
    assert status["user_ai_enabled"] is True
    assert status["user_provider"] == "deepseek"


def test_activate_is_scoped_to_own_keys(client, db, op_headers, adm_headers):
    # 其他用户(管理员)的配置不能被操作者切换,也不会出现在其生效链路里
    client.put(
        "/api/ai/providers/deepseek/key",
        json={"api_key": "sk-admin", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    resp = client.put("/api/ai/keys/deepseek/active", headers=op_headers)
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]
    row = db.query(UserAiKey).filter(UserAiKey.provider == "deepseek").one()
    assert row.enabled is True  # 管理员自己的行不受影响


# ===== 管理员全局配置 =====

def test_global_config_requires_admin(client, op_headers):
    assert client.get("/api/admin/ai", headers=op_headers).status_code == 403
    resp = client.put(
        "/api/admin/ai",
        json={"enabled": True, "provider": "deepseek", "api_key": "sk-x"},
        headers=op_headers,
    )
    assert resp.status_code == 403


def test_global_config_save_encrypts_and_hides_key(client, db, adm_headers):
    resp = client.put(
        "/api/admin/ai",
        json={
            "enabled": True,
            "provider": "mimo",
            "api_key": "sk-global-secret",
            "model": "mimo-v2.6-pro",
        },
        headers=adm_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["has_key"] is True
    assert "sk-global-secret" not in resp.text

    from app.models import GlobalAiConfig

    row = db.get(GlobalAiConfig, 1)
    assert row is not None and "sk-global-secret" not in row.api_key_encrypted

    read = client.get("/api/admin/ai", headers=adm_headers)
    assert read.status_code == 200
    assert "sk-global-secret" not in read.text


def test_global_config_enable_requires_key(client, adm_headers):
    resp = client.put(
        "/api/admin/ai",
        json={"enabled": True, "provider": "deepseek", "api_key": "", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_global_config_applies_to_users_without_own_key(db, operator):
    from app.ai.keys import resolve_effective_ai, save_global_config

    settings = _settings()
    admin = User(
        username="global_admin",
        display_name="管理员",
        password_hash="x",
        role=Role.admin,
    )
    db.add(admin)
    db.commit()

    assert resolve_effective_ai(db, operator, settings) is None
    save_global_config(
        db, admin, settings, enabled=True, provider="deepseek", api_key="sk-global", model="deepseek-flash"
    )
    db.commit()
    config = resolve_effective_ai(db, operator, settings)
    assert config is not None and config.provider == "deepseek" and config.api_key == "sk-global"


def test_user_config_takes_precedence_over_global(db, operator):
    from app.ai.keys import resolve_effective_ai, save_global_config

    settings = _settings()
    admin = User(username="a2", display_name="管理员", password_hash="x", role=Role.admin)
    db.add(admin)
    db.commit()
    save_global_config(db, admin, settings, enabled=True, provider="deepseek", api_key="sk-g", model="deepseek-flash")
    save_user_key(db, operator, "mimo", "sk-u", settings, model="mimo-v2.6-pro")
    db.commit()

    config = resolve_effective_ai(db, operator, settings)
    assert config.provider == "mimo" and config.api_key == "sk-u"


def test_global_config_disabled_is_not_used(db, operator):
    from app.ai.keys import resolve_effective_ai, save_global_config

    settings = _settings()
    admin = User(username="a3", display_name="管理员", password_hash="x", role=Role.admin)
    db.add(admin)
    db.commit()
    save_global_config(db, admin, settings, enabled=True, provider="deepseek", api_key="sk-g")
    save_global_config(db, admin, settings, enabled=False, provider="deepseek")
    db.commit()
    assert resolve_effective_ai(db, operator, settings) is None


def test_global_config_rejected_in_local_only(db, operator):
    from app.ai.keys import save_global_config

    admin = User(username="a4", display_name="管理员", password_hash="x", role=Role.admin)
    db.add(admin)
    db.commit()
    with pytest.raises(KeyError_):
        save_global_config(
            db, admin, _settings(local_only=True), enabled=True, provider="deepseek", api_key="sk-g"
        )


def test_ai_status_reports_cloud_when_global_enabled(client, db, adm_headers):
    client.put(
        "/api/admin/ai",
        json={"enabled": True, "provider": "deepseek", "api_key": "sk-global", "model": "deepseek-flash"},
        headers=adm_headers,
    )
    # 用另一个没有自填 Key 的用户查询状态
    other = User(username="no_key_user", display_name="无Key", password_hash="x", role=Role.operator)
    db.add(other)
    db.commit()
    from app.security import hash_password

    other.password_hash = hash_password("test-password-1")
    db.commit()
    login = client.post("/api/auth/login", json={"username": "no_key_user", "password": "test-password-1"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    status = client.get("/api/system/ai-status", headers=headers).json()
    assert status["ai_mode"] == "cloud"
    assert status["user_ai_enabled"] is False
    assert "管理员配置" in status["message"]

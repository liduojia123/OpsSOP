"""第四阶段第三批:生产配置在迁移前失败的真实入口前置检查。"""

import pytest

from app.config import Settings


def _prod(**overrides) -> Settings:
    base = dict(
        opssop_env="production",
        secret_key="S" * 40,
        opssop_admin_password="StrongAdminPass123456",
        opssop_seed_demo=False,
        ai_mode="disabled",
        embedding_mode="disabled",
        local_only=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_strong_production_config_passes_preflight():
    _prod().validate_production_config()
    _prod().validate_ai_config()


@pytest.mark.parametrize(
    "overrides",
    [
        {"opssop_admin_password": "change-me-admin-password"},
        {"opssop_admin_password": "short"},
        {"secret_key": "change-me-random-secret"},
        {"secret_key": "short"},
        {"opssop_seed_demo": True},
    ],
)
def test_production_preflight_rejects_weak_or_demo_config(overrides):
    with pytest.raises(ValueError):
        _prod(**overrides).validate_production_config()


def test_development_does_not_require_production_secrets():
    Settings(opssop_env="development").validate_production_config()

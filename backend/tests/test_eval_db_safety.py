"""第四阶段第二批:评测库名/环境在连接前硬校验。"""

import pytest

from app.config import Settings
from app.eval.db import (
    EvalSetupError,
    default_target_db,
    validate_run_id,
    validate_target_db,
)


def _settings(**overrides) -> Settings:
    base = dict(
        postgres_db="opssop_business",
        postgres_test_db="opssop_test",
        opssop_env="development",
    )
    base.update(overrides)
    return Settings(**base)


def test_valid_run_scoped_target_is_accepted():
    assert (
        validate_target_db("opssop_eval_run123", _settings(), "run123")
        == "opssop_eval_run123"
    )
    assert default_target_db("run123") == "opssop_eval_run123"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "opssop_business",
        "opssop_test",
        "opssop_eval",
        "opssop_demo",
        "postgres",
        "template0",
        "template1",
        "review_app",
        "review_demo",
        "bad-name",
        "opssop_eval_1;drop",
        "opssop_eval_other",
    ],
)
def test_unsafe_or_foreign_target_is_rejected_before_connection(name):
    with pytest.raises(EvalSetupError):
        validate_target_db(name, _settings(), "run123")


def test_production_is_rejected_before_connection():
    with pytest.raises(EvalSetupError, match="production"):
        validate_target_db(
            "opssop_eval_run123", _settings(opssop_env="production"), "run123"
        )


def test_run_id_must_be_safe_and_bounded():
    assert validate_run_id("run123") == "run123"
    for bad in ["", "Run123", "run", "run-123", "run 123", "x" * 40]:
        with pytest.raises(EvalSetupError):
            validate_run_id(bad)

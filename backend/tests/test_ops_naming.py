"""第四阶段第三批:运行目标/数据库/资源名硬校验。"""

from pathlib import Path

import pytest

from app.ops.common import (
    OpsError,
    validate_db_identifier,
    validate_run_id,
    validate_source_database,
    validate_target_database,
)
from app.ops.dockerops import validate_container_path, validate_project_name
from app.ops.drill import DrillConfig, run_drill


def test_run_id_and_target_names():
    assert validate_run_id("drill20261022a") == "drill20261022a"
    for bad in ("", "Drill20261022", "x", "bad id", "a/b"):
        with pytest.raises(OpsError):
            validate_run_id(bad)
    assert validate_target_database("opssop_restore_20261022") == "opssop_restore_20261022"
    for bad in ("postgres", "template0", "template1", "opssop", "opssop_test", "opssop_eval_x"):
        with pytest.raises(OpsError):
            validate_target_database(bad)


def test_source_and_connection_database_are_separate():
    assert validate_source_database("opssop_prod") == "opssop_prod"
    assert validate_db_identifier("postgres") == "postgres"
    with pytest.raises(OpsError):
        validate_source_database("postgres")
    with pytest.raises(OpsError):
        validate_source_database("opssop_test")


def test_project_and_container_temp_paths():
    assert validate_project_name("opssop-drill-abc") == "opssop-drill-abc"
    for bad in ("mimo", "other-project", "opssop-../x"):
        with pytest.raises(OpsError):
            validate_project_name(bad)
    assert validate_container_path("/tmp/opssop-abc.dump") == "/tmp/opssop-abc.dump"
    for bad in ("/tmp/other.dump", "/tmp/opssop-../x", "/etc/passwd"):
        with pytest.raises(OpsError):
            validate_container_path(bad)


def test_drill_rejects_unignored_in_repo_workdir(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    config = DrillConfig(
        repo_root=repo,
        work_dir=repo / "backend",
        run_id="drillguard1",
    )
    with pytest.raises(OpsError, match="演练产物"):
        run_drill(config)


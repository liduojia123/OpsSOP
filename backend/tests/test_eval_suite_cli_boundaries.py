"""第四阶段第二批复审回归:CLI 外置数据集、非法参数与报告写入失败。

独立复审 P2 #3/#7:
- 外置合法数据集不得在报告路径处理时崩溃,且不泄露绝对路径;
- 非法 run_id 返回配置错误 2 而不是 traceback/1;
- 报告写入失败必须非零退出,stdout 与实际退出码一致;
- 数据集未加载时摘要仍能安全生成。
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

from app.eval.datasets import load_dataset

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATASET_DIR = BACKEND_DIR / "eval_data" / "v1"


def _cli():
    path = BACKEND_DIR / "scripts" / "eval_suite.py"
    spec = importlib.util.spec_from_file_location("review_eval_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_dataset_is_reported_without_absolute_path(tmp_path):
    cli = _cli()
    external = tmp_path / "external_dataset"
    shutil.copytree(DATASET_DIR, external)
    dataset = load_dataset(external)
    info = cli._dataset_report(dataset)
    rendered = json.dumps(info, ensure_ascii=False)
    assert info["path_kind"] == "external"
    assert info["path_relative"] is None
    assert info["path_label"].startswith("external:external_dataset:")
    assert str(tmp_path) not in rendered
    assert str(BACKEND_DIR) not in rendered
    # 内容哈希仍与内置数据集一致
    bundled = load_dataset(DATASET_DIR)
    assert info["dataset_hash"] == bundled.dataset_hash


def test_invalid_run_id_returns_2_and_writes_partial_report(tmp_path, monkeypatch):
    cli = _cli()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_suite.py",
            "--suite",
            "retrieval",
            "--mode",
            "synthetic",
            "--run-id",
            "bad",
            "--out",
            str(tmp_path / "invalid-run"),
        ],
    )
    result = cli.main()
    assert result == 2
    report = json.loads((tmp_path / "invalid-run" / "report.json").read_text(encoding="utf-8"))
    assert report["exit_code"] == 2
    assert report["dataset"] is None
    assert "run_id" in report["error"]
    assert report["requested_run_id_error"]
    summary = (tmp_path / "invalid-run" / "summary.md").read_text(encoding="utf-8")
    assert "未加载" in summary


def test_output_write_failure_returns_nonzero(tmp_path, monkeypatch):
    cli = _cli()

    def unavailable_output(*_args, **_kwargs):
        raise OSError("independent synthetic output write failure")

    monkeypatch.setattr(cli, "_write_outputs", unavailable_output)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_suite.py",
            "--suite",
            "retrieval",
            "--mode",
            "synthetic",
            "--run-id",
            "bad",
            "--out",
            str(tmp_path / "write-failure"),
        ],
    )
    result = cli.main()
    assert result != 0
    assert result == 4


def test_output_path_is_a_file_returns_nonzero(tmp_path):
    cli = _cli()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    report = {
        "run_id": "writefail",
        "dataset": None,
        "environment": {"mode": "synthetic"},
        "counts": {},
        "retrieval": None,
        "analysis": None,
        "exit_code": 0,
        "error": "test",
    }
    try:
        cli._write_outputs(blocker, report)
    except (FileExistsError, NotADirectoryError, OSError):
        pass
    else:  # pragma: no cover
        raise AssertionError("writing into a regular file must fail")

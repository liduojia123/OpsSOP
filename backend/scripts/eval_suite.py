"""第四阶段第二批评测入口:版本化合成检索/分析评测。

默认 synthetic 模式:显式合成适配器、零真实模型/embedding 调用、无 API Key。
真实模式必须显式 --mode real --allow-real;本批不执行真实效果评测。

用法(后端容器内或已配置数据库环境的主机):
    python scripts/eval_suite.py --suite all --mode synthetic
    python scripts/eval_suite.py --suite retrieval --mode synthetic --limit 5
    python scripts/eval_suite.py --cleanup --target-db opssop_eval_<run_id> --run-id <run_id>

退出状态:
    0 = 完成且机制断言通过(不隐含真实模型效果好)
    2 = 配置/数据集/数据库准备错误
    3 = 混合检索降级,整批混合指标不可用/未验证
    4 = 机制断言失败、运行中断或清理失败
    130 = 用户取消(KeyboardInterrupt);保留已完成结果与部分报告后清理并以非零退出
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 允许 `python scripts/eval_suite.py` 直接运行
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.eval.analysis_runner import run_analysis  # noqa: E402
from app.eval.app_context import (  # noqa: E402
    eval_app_context,
    login,
    reset_service_runtime,
    synthetic_settings,
)
from app.eval.datasets import DatasetError, load_dataset, resolve_gold  # noqa: E402
from app.eval.db import (  # noqa: E402
    EvalSetupError,
    create_or_reuse_database,
    database_info,
    default_target_db,
    drop_eval_database,
    validate_run_id,
)
from app.eval.fixtures import EVAL_PASSWORDS, prepare_fixtures  # noqa: E402
from app.eval.retrieval_runner import run_retrieval  # noqa: E402
from app.eval.synthetic import SyntheticChatAdapter, SyntheticEmbeddingAdapter  # noqa: E402
from app.search.embedding_index import get_status, run_build  # noqa: E402

REPORT_SCHEMA_VERSION = "opssop-eval-report/1"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_fingerprint() -> str:
    """对实际参与评测的后端源码/数据集做内容指纹(脏工作区也能标识代码)。"""
    roots = ["app", "scripts", "alembic", "eval_data"]
    digest = hashlib.sha256()
    for root in roots:
        base = BACKEND_DIR / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if "eval-artifacts" in path.parts or "e2e-artifacts" in path.parts:
                continue
            rel = path.relative_to(BACKEND_DIR).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _code_info() -> dict:
    repo_root = BACKEND_DIR.parent
    head = os.environ.get("OPSSOP_EVAL_GIT_HEAD", "unknown")
    status_hash = None
    if (repo_root / ".git").exists():
        try:
            head = subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True
            ).strip()
            status = subprocess.check_output(
                ["git", "-C", str(repo_root), "status", "--porcelain=v1", "-uall"],
                text=True,
            )
            status_hash = hashlib.sha256(status.encode("utf-8")).hexdigest()
        except Exception:  # noqa: BLE001
            pass
    return {
        "head": head,
        "git_status_sha256": status_hash,
        "workspace_fingerprint": _workspace_fingerprint(),
        "note": "脏工作区不能只用 HEAD 标识;workspace_fingerprint 覆盖 app/scripts/alembic/eval_data。",
    }


def _dataset_path_info(path: Path) -> dict:
    """报告用安全路径标识:内置相对路径,外置只记录名称+哈希,不写绝对路径。"""
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(BACKEND_DIR.resolve()).as_posix()
        return {
            "path_kind": "bundled",
            "path_relative": rel,
            "path_label": rel,
        }
    except ValueError:
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        return {
            "path_kind": "external",
            "path_relative": None,
            "path_label": f"external:{path.name}:{digest}",
            "path_sha256": digest,
        }


def _dataset_report(dataset) -> dict:
    path_info = _dataset_path_info(dataset.path)
    return {
        "schema_version": dataset.schema_version,
        "dataset_version": dataset.dataset_version,
        "dataset_hash": dataset.dataset_hash,
        "knowledge_hash": dataset.knowledge_hash,
        "behaviours_hash": dataset.behaviours_hash,
        "file_hashes": dataset.file_hashes,
        "sample_count": len(dataset.samples),
        "knowledge_count": len(dataset.knowledge),
        "label_status": dataset.label_status,
        "label_source": dataset.label_source,
        "human_reviewed": dataset.human_reviewed,
        "description": dataset.description,
        **path_info,
        "groups": {
            group: len([s for s in dataset.samples if s.group == group])
            for group in sorted({s.group for s in dataset.samples})
        },
        "tasks": {
            task: len(dataset.samples_for(task))
            for task in sorted({task for s in dataset.samples for task in s.tasks})
        },
    }


def _build_index(eval_db, settings, embedding_adapter) -> dict:
    db = eval_db.session_factory()
    try:
        outcome = run_build(db, settings, adapter=embedding_adapter, force=True)
        status = get_status(db, settings)
    finally:
        db.close()
    if outcome.result != "success":
        raise EvalSetupError(
            f"合成索引构建未成功({outcome.result}),混合指标不可用;{outcome.error_message}"
        )
    return {
        "result": outcome.result,
        "built_units": outcome.built_units,
        "reused_units": outcome.reused_units,
        "embedded_calls": outcome.embedded_calls,
        "usage": outcome.usage,
        "skipped": outcome.skipped,
        "status": status,
        "source": "synthetic-eval-embedding(机制验证,非真实语义)",
    }


def _metric_line(name: str, metric: dict) -> str:
    value = metric.get("value")
    status = metric.get("status")
    num = metric.get("numerator")
    den = metric.get("denominator")
    if value is None:
        return f"| {name} | - | {num}/{den} | {status} |"
    return f"| {name} | {value} | {num}/{den} | {status} |"


def _empty_metric() -> dict:
    return {
        "numerator": None,
        "denominator": None,
        "value": None,
        "status": "not_available",
        "definition": "",
    }


def _summary_markdown(report: dict) -> str:
    dataset = report.get("dataset") or {}
    environment = report.get("environment") or {}
    dataset_version = dataset.get("dataset_version") or "未加载"
    dataset_hash = dataset.get("dataset_hash") or ""
    dataset_hash_short = f"{dataset_hash[:16]}…" if dataset_hash else "-"
    lines = [
        "# OpsSOP 第四阶段第二批合成评测摘要",
        "",
        f"- run_id: `{report.get('run_id') or 'unknown'}`",
        f"- 数据集: `{dataset_version}` / `{dataset_hash_short}`",
        f"- 数据集路径标识: `{dataset.get('path_label') or '-'}`({dataset.get('path_kind') or 'unknown'})",
        f"- 模式: `{environment.get('mode') or 'unknown'}`(synthetic=true,real=false)",
        f"- 标签: `{dataset.get('label_status') or 'unknown'}` / human_reviewed={dataset.get('human_reviewed')}",
        f"- 退出码: `{report.get('exit_code')}`",
        "",
        "## 说明",
        "",
        "- 本报告是合成协议/指标机制验证,不是真实模型分类质量、真实 embedding 语义效果或真人审核结论。",
        "- 参考标签由开发 AI 编写,状态为 provisional,未经独立人工复核。",
        "- 耗时来自进程内 TestClient,不是生产网络端到端性能。",
        f"- 数据集未加载/配置错误时,本摘要仍会生成;错误: {report.get('error') or '无'}",
        "",
    ]
    if report.get("write_errors"):
        lines += ["## 报告写入问题", ""]
        for item in report["write_errors"]:
            lines.append(f"- `{item.get('artifact')}`: {item.get('error')}")
        lines.append("")
    retrieval = report.get("retrieval")
    if retrieval:
        lines += [
            "## 检索指标(真实 GET /api/search;合成混合向量仅机制验证,非真实语义效果)",
            "",
            f"- 完成: `{retrieval.get('complete')}`;混合指标有效: `{retrieval.get('hybrid_valid')}`;"
            f"降级查询: {retrieval.get('hybrid_degraded_queries')}/{retrieval.get('executed_queries')}",
            "",
        ]
        if retrieval.get("interrupted"):
            lines += [
                f"- 中断: {retrieval['interrupted'].get('stage')}/"
                f"{retrieval['interrupted'].get('sample_id')} "
                f"{retrieval['interrupted'].get('error_type')};"
                f" {retrieval.get('unexecuted_reason', '')}",
                "",
            ]
        metrics = retrieval.get("metrics") or {}
        for mode in ("keyword", "hybrid"):
            stats = metrics.get(mode) or {}
            lines.append(f"### {mode}")
            lines.append("")
            lines.append("| 指标 | 值 | 分子/分母 | 状态 |")
            lines.append("|---|---:|---:|---|")
            for key in ("recall@3", "recall@5", "mrr", "no_answer_accuracy"):
                lines.append(_metric_line(key, stats.get(key) or _empty_metric()))
            lines.append("")
        degrade = retrieval.get("hybrid_degrade_rate") or _empty_metric()
        lines += [
            f"降级率: {degrade.get('numerator')}/{degrade.get('denominator')} "
            f"({degrade.get('status')})",
            "",
        ]
    analysis = report.get("analysis")
    if analysis:
        c = ((analysis.get("classification") or {}).get("main")) or {}
        main_failures = (analysis.get("failures") or {}).get("main") or {}
        lines += [
            "## 分析指标(真实 POST /api/faults/{id}/analyze + 合成适配器;非真实模型质量)",
            "",
            f"- 完成: `{analysis.get('complete')}`",
            "",
            "| 指标 | 值 | 分子/分母 | 状态 |",
            "|---|---:|---:|---|",
            _metric_line("主分类正确率", c.get("accuracy") or _empty_metric()),
            _metric_line("有效建议覆盖率", c.get("suggestion_coverage") or _empty_metric()),
            _metric_line("应弃答正确率", c.get("abstain_accuracy") or _empty_metric()),
            _metric_line("主 suite 失败率", main_failures.get("failure_rate") or _empty_metric()),
            "",
            f"- 主 suite 失败/不完整: {main_failures.get('business_failed')} failed, "
            f"{main_failures.get('incomplete')} incomplete",
        ]
        if analysis.get("interrupted"):
            lines += [
                f"- 中断: {analysis['interrupted'].get('stage')}/"
                f"{analysis['interrupted'].get('sample_id')} "
                f"{analysis['interrupted'].get('error_type')};"
                f" {analysis.get('unexecuted_reason', '')}",
            ]
        main_refs = ((analysis.get("references") or {}).get("main")) or {}
        raw_adopted = main_refs.get("raw_adopted") or {}
        raw_validity = raw_adopted.get("validity") or _empty_metric()
        failure_refs = ((analysis.get("references") or {}).get("failure")) or {}
        failure_raw = failure_refs.get("raw_adopted") or {}
        failure_repair = failure_refs.get("format_repair") or {}
        final_refs = main_refs.get("final") or {}
        final_validity = final_refs.get("validity") or _empty_metric()
        lines += [
            f"- 原始引用有效率(采用输出): "
            f"{raw_validity.get('numerator')}/{raw_validity.get('denominator')} "
            f"{raw_validity.get('status')}",
            f"- 最终展示引用有效率: "
            f"{final_validity.get('numerator')}/{final_validity.get('denominator')} "
            f"{final_validity.get('status')};"
            f" 可观察样本 {final_refs.get('observable_samples')}/"
            f"{final_refs.get('observable_samples') + final_refs.get('unobservable_samples')}"
            if final_refs
            else "- 最终展示引用有效率: 不可用",
            f"- 无法观察原始引用样本: {main_refs.get('unobservable_samples')}",
            f"- 失败注入原始引用: "
            f"{failure_raw.get('valid_occurrences')}/"
            f"{failure_raw.get('occurrences')} valid;"
            f" 不可观察样本 {failure_refs.get('unobservable_samples')};"
            f" 格式修复 {failure_repair.get('samples')} 例"
            f"(成功 {failure_repair.get('repaired_success')}/"
            f"失败 {failure_repair.get('repair_failed')})",
            f"- 缓存 suite 新增调用: "
            f"{(analysis.get('failures') or {}).get('cache', {}).get('new_model_calls')};"
            f" 来源探测请求: "
            f"{(analysis.get('cache_probe') or {}).get('api_requests_during_source_probe')}",
        ]
        counts = analysis.get("assertion_counts") or {}
        lines += [
            f"- 机制断言: {counts.get('passed')} passed / {counts.get('failed')} failed / "
            f"{counts.get('skipped')} skipped",
            "",
            "### 失败注入断言",
            "",
        ]
        for assertion in analysis.get("mechanism_assertions") or []:
            if assertion.get("status") == "skipped":
                mark = "SKIP"
            else:
                mark = "PASS" if assertion.get("passed") else "FAIL"
            lines.append(f"- `{assertion.get('name')}`: {mark} {assertion.get('detail', '')}")
        lines.append("")
        cache_probe = analysis.get("cache_probe") or {}
        if cache_probe:
            lines += [
                f"- 缓存来源探测: `{cache_probe.get('status')}` / path=`{cache_probe.get('path')}` / "
                f"passed={cache_probe.get('passed')} / {cache_probe.get('reason', '')}",
                "",
            ]
    lines += [
        "## 未验证/限制",
        "",
        "- 真实模型质量、真实 embedding、Linux/HTTPS、备份恢复未验证。",
        "- 预置夹具/合成标签不是真人复核证据;真实人工修改比例不可用。",
        "- 本批不代表第四阶段整体验收完成。",
        "",
    ]
    return "\n".join(lines)


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _write_outputs(out_dir: Path, report: dict) -> dict:
    """尝试写 JSON 与摘要;任一失败都返回错误,由调用方转为非零退出。

    先写摘要再写 JSON,这样摘要失败时 JSON 仍可带上 write_errors;
    若 JSON 失败,再尽力用更新后的状态重写摘要。写入失败绝不返回成功。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    summary_path = out_dir / "summary.md"
    result = {
        "ok": True,
        "report_file": None,
        "summary_file": None,
        "errors": [],
    }

    def write_report() -> None:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        result["report_file"] = str(report_path)

    def write_summary() -> None:
        summary_path.write_text(_summary_markdown(report), encoding="utf-8")
        result["summary_file"] = str(summary_path)

    try:
        write_summary()
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"artifact": "summary", "error": _safe_error(exc)})

    try:
        write_report()
    except Exception as exc:  # noqa: BLE001
        result["errors"].append({"artifact": "report", "error": _safe_error(exc)})

    if result["errors"]:
        result["ok"] = False
        report["write_errors"] = result["errors"]
        report["exit_code"] = 4
        # 尽量让已成功写出的产物反映失败状态;再次失败不掩盖原始错误。
        if result["summary_file"]:
            try:
                write_summary()
            except Exception:  # noqa: BLE001
                pass
        if result["report_file"]:
            try:
                write_report()
            except Exception:  # noqa: BLE001
                pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsSOP 第四阶段第二批合成评测")
    parser.add_argument("--dataset", default=str(BACKEND_DIR / "eval_data" / "v1"))
    parser.add_argument("--suite", choices=["all", "retrieval", "analysis"], default="all")
    parser.add_argument("--mode", choices=["synthetic", "real"], default="synthetic")
    parser.add_argument("--allow-real", action="store_true", help="显式允许真实模型调用(本批不执行)")
    parser.add_argument("--run-id", default="", help="本轮 run_id(默认自动生成)")
    parser.add_argument("--target-db", default="", help="评测库名;默认 opssop_eval_<run_id>")
    parser.add_argument("--reuse", action="store_true", help="复用 manifest 匹配的既有评测库")
    parser.add_argument("--keep-db", action="store_true", help="运行后保留本轮评测库以便检查")
    parser.add_argument("--limit", type=int, default=0, help="仅用于快速冒烟;0=全部样本")
    parser.add_argument("--out", default="", help="输出目录;默认 scripts/eval-artifacts/<run_id>")
    parser.add_argument("--cleanup", action="store_true", help="只清理匹配本轮 run_id 的评测库")
    args = parser.parse_args()

    if args.cleanup:
        if not args.target_db or not args.run_id:
            print("--cleanup 需要显式 --target-db 和 --run-id", file=sys.stderr)
            return 2
        try:
            result = drop_eval_database(get_settings(), args.target_db, args.run_id)
            print(json.dumps({"cleanup": result}, ensure_ascii=False, indent=2))
            return 0
        except EvalSetupError as exc:
            print(f"清理拒绝: {exc}", file=sys.stderr)
            return 2

    if args.mode == "real" and not args.allow_real:
        print(
            "真实模式未授权:本批默认只执行合成协议评测。"
            "真实效果入口需显式 --mode real --allow-real;当前不执行真实模型调用。",
            file=sys.stderr,
        )
        return 2
    if args.mode == "real":
        print(
            "真实模式入口存在但本批未执行:真实模型质量/embedding 效果仍需另行授权与数据。",
            file=sys.stderr,
        )
        return 2

    requested_run_id = args.run_id or f"eval{datetime.now().strftime('%m%d')}{uuid.uuid4().hex[:8]}"
    run_id_error: EvalSetupError | None = None
    try:
        run_id = validate_run_id(requested_run_id)
    except EvalSetupError as exc:
        run_id_error = exc
        run_id = "invalid"
    target_db = "" if run_id_error is not None else (args.target_db or default_target_db(run_id))
    out_dir = (
        Path(args.out)
        if args.out
        else BACKEND_DIR.parent
        / "scripts"
        / "eval-artifacts"
        / ("invalid-run-id" if run_id_error is not None else run_id)
    )
    started = _iso()
    report: dict = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "requested_run_id_error": str(run_id_error) if run_id_error is not None else None,
        "started_at": started,
        "finished_at": None,
        "code": _code_info(),
        "dataset": None,
        "environment": {
            "mode": "synthetic",
            "synthetic": True,
            "demo": False,
            "real": False,
            "ai_mode": "synthetic-eval(cloud config shape, adapter injected)",
            "embedding_mode": "synthetic-eval(cloud config shape, adapter injected)",
            "real_chat_calls": 0,
            "real_embedding_calls": 0,
            "api_timing_method": "in-process TestClient",
        },
        "counts": {},
        "retrieval": None,
        "analysis": None,
        "limitations": [
            "合成适配器只验证路由/校验/引用/指标机制,不代表真实模型质量或语义检索效果。",
            "参考标签为开发 AI 编写的 provisional 合成标签,不是真人审核 gold。",
            "真实人工修改比例不可用;没有真人复核记录。",
            "耗时是进程内 API 耗时,不是生产网络端到端性能。",
            "未验证真实聊天模型、真实 embedding、Linux/HTTPS、备份恢复。",
        ],
        "exit_code": None,
        "cleanup": None,
        "setup_cleanup": None,
        "error": None,
    }
    eval_db = None
    exit_code = 0
    try:
        if run_id_error is not None:
            raise run_id_error
        dataset = load_dataset(args.dataset)
        report["dataset"] = _dataset_report(dataset)
        base_settings = get_settings()
        settings = synthetic_settings(base_settings)
        report["environment"].update(
            {
                "ai_model": settings.ai_model,
                "embedding_model": settings.embedding_model,
                "ai_config_version": settings.ai_config_version,
                "embedding_config_fingerprint": settings.embedding_mode,
                "eval_rate_limits": {
                    "ai_rate_limit_per_minute": settings.ai_rate_limit_per_minute,
                    "ai_max_concurrent_per_user": settings.ai_max_concurrent_per_user,
                    "embedding_rate_limit_per_minute": settings.embedding_rate_limit_per_minute,
                    "embedding_max_concurrent_per_user": settings.embedding_max_concurrent_per_user,
                    "note": "仅在隔离评测进程显式设置并记录,不修改产品默认限流",
                },
            }
        )
        eval_db = create_or_reuse_database(
            base_settings,
            target_db,
            run_id=run_id,
            dataset_version=dataset.dataset_version,
            dataset_hash=dataset.dataset_hash,
            reuse=args.reuse,
        )
        report["environment"]["database"] = database_info(base_settings, eval_db.name)
        fixture_db = eval_db.session_factory()
        try:
            fixtures = prepare_fixtures(fixture_db, dataset, eval_db.manifest)
        finally:
            fixture_db.close()
        resolved_gold = resolve_gold(dataset, fixtures.fixture_map)
        chat_adapter = SyntheticChatAdapter(dataset)
        embedding_adapter = SyntheticEmbeddingAdapter()
        index_info = _build_index(eval_db, settings, embedding_adapter)
        report["environment"]["index"] = index_info
        report["environment"]["embedding_config_fingerprint"] = (
            index_info.get("status", {}).get("config_fingerprint") or ""
        )

        reset_service_runtime()
        retrieval_report = None
        analysis_report = None
        retrieval_exit = 0
        analysis_exit = 0
        with eval_app_context(
            eval_db.session_factory, settings, chat_adapter, embedding_adapter
        ) as client:
            owner_headers = login(client, "eval_owner", EVAL_PASSWORDS["eval_owner"])
            viewer_headers = login(client, "eval_viewer", EVAL_PASSWORDS["eval_viewer"])
            if args.suite in ("all", "retrieval"):
                try:
                    retrieval_run = run_retrieval(
                        client,
                        viewer_headers,
                        dataset,
                        resolved_gold,
                        embedding_adapter,
                        limit=max(0, args.limit) if args.limit else 0,
                    )
                    retrieval_report = retrieval_run.report
                    retrieval_exit = retrieval_run.exit_code
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    planned = len(dataset.samples_for("retrieval"))
                    cancelled = isinstance(exc, KeyboardInterrupt)
                    retrieval_report = {
                        "complete": False,
                        "cancelled": cancelled,
                        "interrupted": {
                            "stage": "retrieval",
                            "sample_id": None,
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:300],
                        },
                        "unexecuted_reason": (
                            f"检索运行器{'被用户取消' if cancelled else '异常'}:"
                            f"{type(exc).__name__};未执行查询保留为未执行"
                        ),
                        "planned_queries": planned,
                        "executed_queries": 0,
                        "unexecuted_queries": planned,
                        "limit": args.limit,
                        "hybrid_valid": False,
                        "hybrid_degraded_queries": 0,
                        "hybrid_degrade_rate": {
                            "numerator": 0,
                            "denominator": 0,
                            "value": None,
                            "status": "partial",
                            "definition": "运行器异常,无降级率",
                        },
                        "metrics": {},
                        "groups": {},
                        "cases": [],
                        "timing": {},
                    }
                    retrieval_exit = 130 if cancelled else 4
            retrieval_cancelled = bool(
                retrieval_report and retrieval_report.get("cancelled")
            )
            if args.suite in ("all", "analysis") and not retrieval_cancelled:
                try:
                    analysis_run = run_analysis(
                        client,
                        owner_headers,
                        viewer_headers,
                        dataset,
                        fixtures,
                        chat_adapter,
                        eval_db.session_factory,
                        limit=max(0, args.limit) if args.limit else 0,
                    )
                    analysis_report = analysis_run.report
                    analysis_exit = analysis_run.exit_code
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    planned = {
                        "main": len(dataset.samples_for("analysis")),
                        "failure": len(dataset.samples_for("analysis_failure")),
                        "cache": len(dataset.samples_for("analysis_cache")) * 2,
                        "permission": len(dataset.samples_for("analysis_permission")),
                    }
                    cancelled = isinstance(exc, KeyboardInterrupt)
                    analysis_report = {
                        "suite": "analysis",
                        "complete": False,
                        "cancelled": cancelled,
                        "interrupted": {
                            "stage": "analysis",
                            "sample_id": None,
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:300],
                        },
                        "unexecuted_reason": (
                            f"分析运行器{'被用户取消' if cancelled else '异常'}:"
                            f"{type(exc).__name__};未执行请求保留为未执行"
                        ),
                        "planned": planned,
                        "executed": {key: 0 for key in planned},
                        "unexecuted": dict(planned),
                        "limit": args.limit,
                        "classification": {},
                        "references": {},
                        "timing": {},
                        "failures": {},
                        "usage": {},
                        "cache_probe": {},
                        "cache_probes": [],
                        "injection": {},
                        "mechanism_assertions": [],
                        "assertion_counts": {
                            "passed": 0,
                            "failed": 0,
                            "skipped": 0,
                            "total": 0,
                        },
                        "cases": [],
                    }
                    analysis_exit = 130 if cancelled else 4

        report["retrieval"] = retrieval_report
        report["analysis"] = analysis_report
        report["environment"]["synthetic_chat_calls"] = len(chat_adapter.calls)
        report["environment"]["synthetic_embedding_calls"] = len(embedding_adapter.calls)
        report["environment"]["synthetic_chat"] = chat_adapter.describe()
        report["environment"]["synthetic_embedding"] = embedding_adapter.describe()
        run_retrieval_suite = args.suite in ("all", "retrieval")
        run_analysis_suite = args.suite in ("all", "analysis")
        retrieval_planned = len(dataset.samples_for("retrieval"))
        analysis_planned = (
            len(dataset.samples_for("analysis"))
            + len(dataset.samples_for("analysis_failure"))
            + len(dataset.samples_for("analysis_cache")) * 2
            + len(dataset.samples_for("analysis_permission"))
        )
        report["counts"] = {
            "dataset_samples": len(dataset.samples),
            "retrieval_planned": retrieval_planned if run_retrieval_suite else 0,
            "retrieval_executed": (
                len(retrieval_report["cases"]) if retrieval_report else 0
            ),
            "analysis_planned": analysis_planned if run_analysis_suite else 0,
            "analysis_executed": (
                len(analysis_report["cases"]) if analysis_report else 0
            ),
            "retrieval_unexecuted": (
                0
                if not run_retrieval_suite
                else int(retrieval_report.get("unexecuted_queries", 0))
                if retrieval_report
                else retrieval_planned
            ),
            "analysis_unexecuted": (
                0
                if not run_analysis_suite
                else max(0, analysis_planned - len(analysis_report.get("cases") or []))
                if analysis_report
                else analysis_planned
            ),
            "limit": args.limit,
        }
        report["cancelled"] = bool(
            (retrieval_report or {}).get("cancelled")
            or (analysis_report or {}).get("cancelled")
        )
        exit_code = max(retrieval_exit, analysis_exit)
    except KeyboardInterrupt as exc:
        # 可捕获的用户取消:写部分报告、清理后按明确契约以 130 退出。
        report["cancelled"] = True
        report["error"] = f"KeyboardInterrupt: {str(exc)[:300]}"
        cleanup_info = getattr(exc, "eval_cleanup", None)
        if cleanup_info is not None:
            report["setup_cleanup"] = cleanup_info
        exit_code = 130
    except (DatasetError, EvalSetupError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        cleanup_info = getattr(exc, "eval_cleanup", None)
        if cleanup_info is not None:
            report["setup_cleanup"] = cleanup_info
        exit_code = 2 if not cleanup_info or cleanup_info.get("dropped", True) else 4
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        cleanup_info = getattr(exc, "eval_cleanup", None)
        if cleanup_info is not None:
            report["setup_cleanup"] = cleanup_info
        exit_code = 4
    finally:
        report["finished_at"] = _iso()
        if eval_db is not None and not args.keep_db:
            try:
                report["cleanup"] = drop_eval_database(
                    get_settings(), eval_db.name, run_id
                )
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                cancelled_cleanup = isinstance(exc, KeyboardInterrupt)
                report["cleanup"] = {
                    "dropped": False,
                    "status": "cancelled" if cancelled_cleanup else "failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
                if cancelled_cleanup:
                    report["cancelled"] = True
                    exit_code = 130
                else:
                    exit_code = max(exit_code, 4)
        elif eval_db is not None:
            report["cleanup"] = {
                "dropped": False,
                "status": "kept",
                "database": eval_db.name,
            }
        report["exit_code"] = exit_code
        # 清理失败/配置错误也保留报告;输出路径在仓库外/Git 忽略目录。
        # 输出失败必须非零退出,不能把未交付产物写成成功。
        try:
            output = _write_outputs(out_dir, report)
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
            output = {
                "ok": False,
                "report_file": None,
                "summary_file": None,
                "errors": [{"artifact": "output_dir", "error": _safe_error(exc)}],
            }
            if isinstance(exc, KeyboardInterrupt):
                report["cancelled"] = True
                exit_code = 130
        report["report_file"] = output.get("report_file")
        report["summary_file"] = output.get("summary_file")
        if not output.get("ok", False):
            report["write_errors"] = output.get("errors") or []
            report["exit_code"] = 130 if report.get("cancelled") else max(exit_code, 4)
            exit_code = report["exit_code"]
            print(
                "报告写入失败: "
                + "; ".join(
                    f"{item.get('artifact')}={item.get('error')}"
                    for item in report["write_errors"]
                ),
                file=sys.stderr,
            )

    print(json.dumps(
        {
            "run_id": run_id,
            "dataset_version": (report.get("dataset") or {}).get("dataset_version"),
            "mode": report["environment"]["mode"],
            "counts": report["counts"],
            "exit_code": exit_code,
            "cancelled": report.get("cancelled", False),
            "error": report["error"],
            "setup_cleanup": report.get("setup_cleanup"),
            "cleanup": report["cleanup"],
            "report_file": report.get("report_file"),
            "summary_file": report.get("summary_file"),
            "write_errors": report.get("write_errors"),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

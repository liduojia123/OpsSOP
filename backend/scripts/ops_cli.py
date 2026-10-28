"""OpsSOP 运维 CLI:备份、恢复到新库、隔离演练。

用法示例(宿主机,Python 3.10+ 标准库,需要 docker CLI):
  python backend/scripts/ops_cli.py backup \
    --project opssop-demo --env-file .env.demo --run-id backup20261022 \
    --out-dir D:/opssop-backups

  python backend/scripts/ops_cli.py restore \
    --project opssop-demo --env-file .env.demo --run-id restore20261022 \
    --archive D:/opssop-backups/xxx.dump --metadata D:/opssop-backups/xxx.json \
    --target-db opssop_restore_20261022 --out-dir D:/opssop-restore

  python backend/scripts/ops_cli.py drill \
    --work-dir D:/opssop-drill-20261022 --run-id drill20261022
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.ops.backup import run_backup  # noqa: E402
from app.ops.common import OpsCancelled, OpsError, atomic_write_json  # noqa: E402
from app.ops.dockerops import ComposeStack  # noqa: E402
from app.ops.drill import DrillConfig, run_drill  # noqa: E402
from app.ops.restore import run_restore  # noqa: E402


def _print(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _stack(args) -> ComposeStack:
    files = [Path(p) for p in args.compose_file]
    return ComposeStack(
        Path(args.repo_root),
        args.project,
        Path(args.env_file),
        files,
        run_id=args.run_id,
    )


def _backup(args) -> int:
    stack = _stack(args)
    result = run_backup(
        stack,
        out_dir=Path(args.out_dir),
        run_id=args.run_id,
        archive_name=args.name,
        repo_root=Path(args.repo_root),
        image=args.image,
    )
    try:
        atomic_write_json(Path(args.out_dir) / "backup-result.json", result)
    except Exception as exc:  # noqa: BLE001
        _print({"success": False, "error": f"备份结果写入失败: {type(exc).__name__}"})
        return 4
    _print(
        {
            "success": result.get("success"),
            "status": result.get("status"),
            "core_success": result.get("core_success"),
            "cleanup_complete": result.get("cleanup_complete"),
            "run_id": args.run_id,
            "archive_name": result.get("archive_name"),
            "archive_bytes": result.get("archive_bytes"),
            "archive_sha256": result.get("archive_sha256"),
            "alembic_version": result.get("alembic_version"),
            "vector_version": result.get("vector_version"),
            "container_temp_cleanup": result.get("container_temp_cleanup"),
            "warning": result.get("warning"),
        }
    )
    return 0 if result.get("success") else 4


def _restore(args) -> int:
    stack = _stack(args)
    result = run_restore(
        stack,
        archive_path=Path(args.archive),
        metadata_path=Path(args.metadata),
        target_db=args.target_db,
        run_id=args.run_id,
        out_dir=Path(args.out_dir),
        cleanup_failed_target=args.cleanup_failed_target,
        allow_version_mismatch=args.allow_version_mismatch,
        repo_root=Path(args.repo_root),
        image=args.image,
    )
    _print(
        {
            "success": result.get("success"),
            "status": result.get("status"),
            "core_success": result.get("core_success"),
            "cleanup_complete": result.get("cleanup_complete"),
            "run_id": args.run_id,
            "target_database": result.get("target_database"),
            "comparison_equal": (result.get("comparison") or {}).get("equal"),
            "alembic_version": result.get("alembic_version"),
            "target_preserved": result.get("target_preserved"),
            "container_temp_cleanup": result.get("container_temp_cleanup"),
            "warning": result.get("warning"),
        }
    )
    return 0 if result.get("success") else 4


def _drill(args) -> int:
    work_dir = Path(args.work_dir)
    if work_dir.exists() and (work_dir / "result.json").exists():
        _print({"success": False, "error": "工作目录已有 result.json,拒绝覆盖"})
        return 2
    config = DrillConfig(
        repo_root=Path(args.repo_root),
        work_dir=work_dir,
        run_id=args.run_id,
        backend_image=args.backend_image,
        timeout=args.timeout,
        keep_resources=args.keep_resources,
        cleanup_on_failure=args.cleanup_on_failure,
        skip_frontend_build=args.skip_frontend_build,
    )
    result = run_drill(config)
    _print(
        {
            "success": result.get("success"),
            "cancelled": result.get("cancelled"),
            "run_id": result.get("run_id"),
            "error": result.get("error"),
            "result_file": str(work_dir / "result.json"),
            "summary_file": str(work_dir / "summary.md"),
            "steps": {
                step.get("name"): step.get("status")
                for step in result.get("steps", [])
                if step.get("status") != "planned"
            },
            "cleanup": result.get("cleanup"),
        }
    )
    if result.get("cancelled"):
        return 130
    return 0 if result.get("success") else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpsSOP 备份/恢复/演练 CLI")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="pg_dump -Fc 备份 + 元信息")
    backup.add_argument("--project", required=True)
    backup.add_argument("--env-file", required=True)
    backup.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="可重复;默认 docker-compose.yml",
    )
    backup.add_argument("--run-id", required=True)
    backup.add_argument("--out-dir", required=True)
    backup.add_argument("--name", default="")
    backup.add_argument("--image", default="")
    backup.set_defaults(func=_backup)

    restore = sub.add_parser("restore", help="恢复到全新目标库")
    restore.add_argument("--project", required=True)
    restore.add_argument("--env-file", required=True)
    restore.add_argument("--compose-file", action="append", default=[])
    restore.add_argument("--archive", required=True)
    restore.add_argument("--metadata", required=True)
    restore.add_argument("--target-db", required=True)
    restore.add_argument("--run-id", required=True)
    restore.add_argument("--out-dir", required=True)
    restore.add_argument("--cleanup-failed-target", action="store_true")
    restore.add_argument("--allow-version-mismatch", action="store_true")
    restore.add_argument("--image", default="")
    restore.set_defaults(func=_restore)

    drill = sub.add_parser("drill", help="隔离备份/恢复/持久化/生产 smoke 演练")
    drill.add_argument("--work-dir", required=True)
    drill.add_argument("--run-id", required=True)
    drill.add_argument("--backend-image", default="opssop-backend:1.1.2")
    drill.add_argument("--timeout", type=int, default=180)
    drill.add_argument("--keep-resources", action="store_true")
    drill.add_argument("--cleanup-on-failure", action="store_true")
    drill.add_argument("--skip-frontend-build", action="store_true")
    drill.set_defaults(func=_drill)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in ("backup", "restore") and not args.compose_file:
        args.compose_file = [str(Path(args.repo_root) / "docker-compose.yml")]
    try:
        return args.func(args)
    except OpsCancelled as exc:
        _print({"success": False, "cancelled": True, "error": str(exc)})
        return 130
    except OpsError as exc:
        _print({"success": False, "error": str(exc), "stage": exc.stage})
        return 4 if exc.stage in ("backup", "restore", "drill", "verify", "persistence", "cleanup") else 2
    except KeyboardInterrupt:
        _print({"success": False, "cancelled": True, "error": "用户取消"})
        return 130
    except Exception as exc:  # noqa: BLE001
        _print({"success": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

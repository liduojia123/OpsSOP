#!/usr/bin/env python3
"""统一检查入口。

用法（在仓库根目录执行）:
    python scripts/check.py all
    python scripts/check.py frontend
    python scripts/check.py backend

前端：Docker node:24 内执行 npm test 与 npm run build，依赖目录放在命名卷，
      避免宿主 Node 版本或 esbuild 文件锁差异。
后端：临时环境文件 + 独立 Compose 项目（opssop-check）启动数据库并运行 pytest，
      不读取根目录 .env，不接触业务库，结束后删除容器与数据卷。
需要本机可用的 docker compose 2.24+。
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], cwd: Path = ROOT) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def require_docker() -> None:
    if not shutil.which("docker"):
        sys.exit("未找到 docker：请在能访问 Docker 引擎的环境执行（Windows 需启动 Docker Desktop 或进入 WSL）")


def frontend() -> None:
    require_docker()
    run(
        [
            "docker", "run", "--rm",
            "-v", f"{ROOT / 'frontend'}:/work",
            "-w", "/work",
            "-v", "opssop-check-node-modules:/work/node_modules",
            "node:24-alpine",
            "sh", "-lc", "npm ci --no-audit --no-fund && npm test && npm run build",
        ]
    )


def backend() -> None:
    require_docker()

    project = "opssop-check"
    with tempfile.TemporaryDirectory(prefix="opssop-check-") as tmp:
        env_file = Path(tmp) / "check.env"
        env_file.write_text(
            "\n".join(
                [
                    "POSTGRES_USER=opssop",
                    f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
                    "POSTGRES_DB=opssop",
                    # 0 让 Docker 分配临时宿主端口，避免与本机已有 PostgreSQL 冲突
                    "POSTGRES_PORT=0",
                    "POSTGRES_TEST_DB=opssop_check_test",
                    f"SECRET_KEY={secrets.token_urlsafe(32)}",
                    "OPSSOP_ENV=development",
                    f"OPSSOP_ADMIN_PASSWORD={secrets.token_urlsafe(24)}",
                    "OPSSOP_SEED_DEMO=0",
                    "AI_MODE=disabled",
                    "EMBEDDING_MODE=disabled",
                    "LOCAL_ONLY=1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        compose = ["docker", "compose", "-p", project, "--env-file", str(env_file)]
        try:
            run(compose + ["up", "-d", "--wait", "db"])
            run(
                compose
                + ["run", "--rm", "--build", "--no-deps", "backend", "python", "-m", "pytest", "tests/", "-q"]
            )
        finally:
            subprocess.run(compose + ["down", "-v", "--remove-orphans"], cwd=ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpsSOP 统一检查入口")
    parser.add_argument("target", nargs="?", default="all", choices=["frontend", "backend", "all"])
    args = parser.parse_args()

    try:
        if args.target in ("backend", "all"):
            backend()
        if args.target in ("frontend", "all"):
            frontend()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"检查失败（退出码 {exc.returncode}）：{exc.cmd}")


if __name__ == "__main__":
    main()
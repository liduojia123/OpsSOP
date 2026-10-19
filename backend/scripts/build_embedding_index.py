"""embedding 索引构建/重建命令行(第三阶段第二批)。

用法(后端容器内或已配置数据库环境的主机):
    python scripts/build_embedding_index.py            # 构建/刷新(同内容复用向量)
    python scripts/build_embedding_index.py --rebuild  # 强制重建(新代次)
    python scripts/build_embedding_index.py --status   # 仅查看状态(不触发模型调用)

Compose 等价命令:
    docker compose run --rm backend python scripts/build_embedding_index.py --status
    docker compose run --rm backend python scripts/build_embedding_index.py --rebuild

说明:构建/重建会把脱敏后的知识内容发送至配置的 embedding 服务(cloud 模式为出网操作);
状态查询为纯读取,不产生模型调用。输出为 JSON,不含密钥。
"""

import argparse
import json
import os
import sys

# 允许 `python scripts/xxx.py` 直接运行:把应用根目录(/srv/opssop)加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description="embedding 索引构建/状态")
    parser.add_argument("--rebuild", action="store_true", help="强制重建(新代次)")
    parser.add_argument("--status", action="store_true", help="仅查看状态(不调用模型)")
    args = parser.parse_args()

    from app.config import get_settings
    from app.database import SessionLocal
    from app.search.embedding_index import (
        IndexBuildConflict,
        IndexBuildRejected,
        get_status,
        run_build,
    )

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.status:
            snap = get_status(db, settings)
            print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
            return 0
        outcome = run_build(db, settings, force=args.rebuild)
        snap = get_status(db, settings)
        print(
            json.dumps(
                {
                    "result": outcome.result,
                    "built_units": outcome.built_units,
                    "reused_units": outcome.reused_units,
                    "embedded_calls": outcome.embedded_calls,
                    "usage": outcome.usage,
                    "skipped": outcome.skipped,
                    "status": snap,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if outcome.result in ("success", "partial") else 1
    except IndexBuildRejected as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except IndexBuildConflict as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

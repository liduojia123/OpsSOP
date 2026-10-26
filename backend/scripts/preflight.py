"""启动前配置预检:在 Alembic 迁移/种子写入之前失败。

由 scripts/start.sh 在迁移前调用。只做配置检查,不连接数据库、不打印密钥。
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    try:
        settings.validate_production_config()
        settings.validate_ai_config()
    except ValueError as exc:
        print(f"[opssop] 启动前配置检查失败: {exc}", file=sys.stderr)
        return 2
    print("[opssop] 启动前配置检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

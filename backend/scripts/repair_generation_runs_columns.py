"""修复:已执行「旧版 f7e8d9c0b1a2」的数据库补齐 sop_ai_generation_runs 新列。

背景(2026-10-15 第三阶段复审记录):
- f7e8d9c0b1a2(第三阶段第一批)sop_ai_generation_runs 表曾在未发布状态下原地修改,
  增加 attempt / field_sources 两列;
- 若某本地数据库在修改前已执行旧版迁移,alembic_version 已是 f7e8d9c0b1a2,
  单纯 `alembic upgrade head` 不会补齐新列;
- 本脚本幂等补列(ADD COLUMN IF NOT EXISTS),不修改/删除任何既有业务数据。

用法(后端容器内或已配置数据库环境的主机):
    python scripts/repair_generation_runs_columns.py
Compose 等价:
    docker compose run --rm backend python scripts/repair_generation_runs_columns.py

未执行过 f7e8d9c0b1a2 的数据库不需要本脚本(直接 upgrade head 即可);
本脚本对已含新列的数据库为无操作。
"""

import json
import os
import sys

# 允许 `python scripts/xxx.py` 直接运行:把应用根目录(/srv/opssop)加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def main() -> int:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        exists = db.execute(
            text("SELECT to_regclass('public.sop_ai_generation_runs') IS NOT NULL")
        ).scalar()
        if not exists:
            print(
                json.dumps(
                    {
                        "action": "skipped",
                        "reason": "sop_ai_generation_runs 表不存在;请先执行 alembic upgrade head",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        db.execute(
            text(
                "ALTER TABLE sop_ai_generation_runs "
                "ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0"
            )
        )
        db.execute(
            text(
                "ALTER TABLE sop_ai_generation_runs "
                "ADD COLUMN IF NOT EXISTS field_sources jsonb NOT NULL DEFAULT '[]'::jsonb"
            )
        )
        db.commit()
        cols = [
            row[0]
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'sop_ai_generation_runs' ORDER BY ordinal_position"
                )
            )
        ]
        print(json.dumps({"action": "repaired", "columns": cols}, ensure_ascii=False))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""显式生产准备流程:检查/轮换/停用遗留默认口令与演示账号。

在后端容器内执行:
  docker compose exec backend python scripts/prepare_production.py
  docker compose exec backend python scripts/prepare_production.py --disable operator
  docker compose exec backend python scripts/prepare_production.py --rotate admin '新的强口令'

设计原则:
- 只做显式操作,绝不静默改密;
- 报告仅列用户名/角色/问题,不输出口令或哈希;
- 与启动检查(find_insecure_accounts)共用同一判定。
"""

import argparse
import sys

from sqlalchemy import select

sys.path.insert(0, "/srv/opssop")

from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.seed import find_insecure_accounts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="OpsSOP 生产准备:清理遗留默认口令与演示账号")
    parser.add_argument("--disable", metavar="用户名", help="停用指定账号(可多次指定)")
    parser.add_argument(
        "--rotate",
        nargs=2,
        metavar=("用户名", "新口令"),
        action="append",
        default=[],
        help="轮换指定账号口令(可多次指定)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        problems = find_insecure_accounts(db)
        if problems:
            print("发现不安全的活动账号:")
            for p in problems:
                print(f"  - {p}")
        else:
            print("未发现遗留默认口令或演示账号。")

        for username in args.disable or []:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                print(f"错误:账号 {username} 不存在", file=sys.stderr)
                return 1
            if not user.is_active:
                print(f"账号 {username} 已是停用状态,跳过")
                continue
            user.is_active = False
            db.commit()
            print(f"已停用账号: {username}")

        for username, new_password in args.rotate or []:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                print(f"错误:账号 {username} 不存在", file=sys.stderr)
                return 1
            user.password_hash = hash_password(new_password)
            db.commit()
            print(f"已轮换账号口令: {username}(请自行妥善保存新口令)")

        if args.disable or args.rotate:
            remaining = find_insecure_accounts(db)
            if remaining:
                print("仍存在不安全账号:")
                for p in remaining:
                    print(f"  - {p}")
                return 2
            print("账号检查通过,可启动生产模式。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

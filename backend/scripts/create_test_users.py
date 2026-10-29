"""创建/停用测试账号（仅用于本地生产预演或测试环境）。

- 创建：operator/reviewer 已存在时跳过，不覆盖既有口令。口令来自
  TEST_OPERATOR_PASSWORD / TEST_REVIEWER_PASSWORD 环境变量；缺省时生成
  强随机值并写入 --credentials-out 文件（权限 600），不在 stdout 打印，
  避免进入终端日志。
- 停用：`--disable` 把两个测试账号置为不可登录。正式开放前执行；
  不删除账号，保留审计等历史引用。
- 生产保护：OPSSOP_ENV=production 时创建必须显式传 `--allow-test-users`，
  否则拒绝，避免已知口令账号被悄悄带进正式环境。生产正式环境不要保留
  测试账号。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Role, User
from app.security import hash_password

TEST_USERS = ("operator", "reviewer")


def create(db, username: str, display_name: str, role: Role, password: str) -> bool:
    """返回是否新建了账号;已存在时跳过,不覆盖口令。"""
    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        print(f"skip {username}: 已存在 (角色 {existing.role.value}, 启用={existing.is_active})")
        return False
    db.add(
        User(
            username=username,
            display_name=display_name,
            password_hash=hash_password(password),
            role=role,
        )
    )
    print(f"created {username} / {display_name} / {role.value}")
    return True


def disable(db) -> int:
    changed: list[str] = []
    for username in TEST_USERS:
        user = db.scalar(select(User).where(User.username == username))
        if user is not None and user.is_active:
            user.is_active = False
            changed.append(username)
    db.commit()
    if changed:
        print(f"已停用测试账号: {', '.join(changed)} (is_active=False)")
    else:
        print("没有需要停用的测试账号(不存在或已停用)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="创建/停用测试运维/审核账号(预演环境专用)")
    parser.add_argument(
        "--disable",
        action="store_true",
        help="停用测试账号(置为不可登录);正式开放前执行,不删除账号",
    )
    parser.add_argument(
        "--allow-test-users",
        action="store_true",
        help="生产环境显式允许创建测试账号(仅限预演;正式环境不要保留)",
    )
    parser.add_argument(
        "--credentials-out",
        default="test-users-credentials.txt",
        help="随机口令写入的文件路径(默认 ./test-users-credentials.txt,权限 600)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.disable:
            # 停用是安全操作,任何环境都允许,不受生产保护限制
            return disable(db)

        settings = get_settings()
        if settings.is_production and not args.allow_test_users:
            print(
                "拒绝:当前为生产环境(OPSSOP_ENV=production)。"
                "创建测试账号必须显式传 --allow-test-users;正式开放前请执行 --disable 停用测试账号。",
                file=sys.stderr,
            )
            return 2

        operator_password = os.environ.get("TEST_OPERATOR_PASSWORD", "")
        reviewer_password = os.environ.get("TEST_REVIEWER_PASSWORD", "")
        generated: dict[str, str] = {}
        if not operator_password:
            operator_password = secrets.token_urlsafe(12)
            generated["operator"] = operator_password
        if not reviewer_password:
            reviewer_password = secrets.token_urlsafe(12)
            generated["reviewer"] = reviewer_password

        results = [
            ("operator", create(db, "operator", "测试运维", Role.operator, operator_password)),
            ("reviewer", create(db, "reviewer", "测试审核", Role.reviewer, reviewer_password)),
        ]
        db.commit()
        fresh = {username: generated[username] for username, created_now in results if created_now and username in generated}
        if fresh:
            out = Path(args.credentials_out)
            out.write_text(
                "\n".join(f"{username}: {password}" for username, password in fresh.items()) + "\n",
                encoding="utf-8",
            )
            out.chmod(0o600)
            print(f"随机口令已写入 {out} (权限 600,请勿提交或外传;stdout 不打印口令)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

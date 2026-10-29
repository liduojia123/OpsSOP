"""创建/检查测试账号（仅用于本地生产预演或测试环境）。

账号已存在时跳过，不覆盖既有口令；口令来自环境变量，缺省时生成强随机值
并只打印一次。生产正式环境不要保留测试账号。
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

from app.database import SessionLocal
from app.models import Role, User
from app.security import hash_password


def create(db, username: str, display_name: str, role: Role, password: str) -> bool:
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
    print(f"created {username} / {display_name} / {role.value} / password={password}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="创建测试运维/审核账号")
    parser.add_argument("--operator-password", default=os.environ.get("TEST_OPERATOR_PASSWORD", ""))
    parser.add_argument("--reviewer-password", default=os.environ.get("TEST_REVIEWER_PASSWORD", ""))
    args = parser.parse_args()

    operator_password = args.operator_password or secrets.token_urlsafe(12)
    reviewer_password = args.reviewer_password or secrets.token_urlsafe(12)
    db = SessionLocal()
    try:
        create(db, "operator", "测试运维", Role.operator, operator_password)
        create(db, "reviewer", "测试审核", Role.reviewer, reviewer_password)
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

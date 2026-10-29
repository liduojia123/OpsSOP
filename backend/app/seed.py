"""可重复初始化:内置分类与初始账号。幂等,可多次执行。

账号语义(重要):
- 环境变量中的口令仅用于**首次创建**账号;已存在的账号绝不在启动时被覆盖或静默改密。
- 修改已有口令/停用账号必须通过账号管理(users API)或 scripts/prepare_production.py 显式执行。
- 生产启动时检查遗留默认口令与演示账号,发现问题则拒绝启动并提示处理。
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database import SessionLocal
from app.models import Category, Role, User
from app.security import hash_password, verify_password

logger = logging.getLogger("opssop.seed")

SYSTEM_CATEGORIES = [
    ("Kubernetes", 10),
    ("Linux", 20),
    ("网络与 DNS", 30),
    ("数据库与缓存", 40),
    ("应用与中间件", 50),
    ("证书", 60),
    ("CI/CD", 70),
    ("待分类", 90),
]

# 生产环境禁止使用的弱口令(含示例演示口令)
WEAK_PASSWORDS = {
    "change-me-admin-password",
    "change-me-strong-password",
    "change-me-random-secret",
    "demo-operator-password",
    "demo-reviewer-password",
    "admin",
    "password",
    "12345678",
}

# 演示种子账号的指纹(用户名 + 种子显示名),生产环境不应保留
DEMO_SEED_USERNAMES = {"operator", "reviewer"}
DEMO_SEED_DISPLAY_NAMES = {"演示运维", "演示审核"}


def find_insecure_accounts(db: Session) -> list[str]:
    """返回仍不安全的活动账号描述列表(遗留默认口令 / 演示种子账号)。"""
    problems: list[str] = []
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    for user in users:
        for weak in WEAK_PASSWORDS:
            if verify_password(weak, user.password_hash):
                problems.append(
                    f"账号 {user.username}(角色 {user.role.value})仍在使用示例默认口令,须轮换或停用"
                )
                break
        if user.username in DEMO_SEED_USERNAMES and user.display_name in DEMO_SEED_DISPLAY_NAMES:
            problems.append(
                f"账号 {user.username}({user.display_name})是演示种子账号,须停用或改造为正式账号"
            )
    return problems


def ensure_seed(session_factory: sessionmaker = SessionLocal) -> None:
    settings = get_settings()
    if settings.is_production:
        weak = {
            name
            for name, value in [
                ("OPSSOP_ADMIN_PASSWORD", settings.opssop_admin_password),
                ("SECRET_KEY", settings.secret_key),
            ]
            if value in WEAK_PASSWORDS
        }
        if weak:
            raise RuntimeError(
                f"生产环境禁止使用示例默认值: {', '.join(weak)}。请在 .env 中设置强随机值后重启。"
            )
        if settings.opssop_seed_demo:
            raise RuntimeError("生产环境禁止 OPSSOP_SEED_DEMO=1(演示账号)。")

    db = session_factory()
    try:
        for name, sort_order in SYSTEM_CATEGORIES:
            exists = db.scalar(select(Category).where(Category.name == name))
            if not exists:
                db.add(Category(name=name, is_system=True, sort_order=sort_order))
                logger.info("创建内置分类: %s", name)

        admin = db.scalar(select(User).where(User.username == settings.opssop_admin_username))
        if not admin:
            db.add(
                User(
                    username=settings.opssop_admin_username,
                    display_name="管理员",
                    password_hash=hash_password(settings.opssop_admin_password),
                    role=Role.admin,
                )
            )
            logger.info(
                "首次创建管理员账号: %s(口令来自 OPSSOP_ADMIN_PASSWORD,仅此一次生效)",
                settings.opssop_admin_username,
            )
        else:
            logger.info(
                "管理员账号 %s 已存在,跳过创建;环境变量口令不会覆盖已有口令,轮换请走账号管理",
                settings.opssop_admin_username,
            )

        if settings.opssop_seed_demo and not settings.is_production:
            for username, display_name, role, password in [
                ("operator", "演示运维", Role.operator, settings.opssop_demo_operator_password),
                ("reviewer", "演示审核", Role.reviewer, settings.opssop_demo_reviewer_password),
            ]:
                if not db.scalar(select(User).where(User.username == username)):
                    db.add(
                        User(
                            username=username,
                            display_name=display_name,
                            password_hash=hash_password(password),
                            role=role,
                        )
                    )
                    logger.info("创建演示账号: %s", username)
        db.commit()

        if settings.is_production:
            problems = find_insecure_accounts(db)
            if problems:
                raise RuntimeError(
                    "生产启动被阻止:发现遗留的不安全账号。\n"
                    + "\n".join(f"  - {p}" for p in problems)
                    + "\n请在后端容器执行显式生产准备流程:"
                    "\n  python scripts/prepare_production.py            # 查看报告"
                    "\n  python scripts/prepare_production.py --disable <用户名>"
                    "\n  python scripts/prepare_production.py --rotate <用户名> <新口令>"
                    "\n口令轮换/停用均为显式操作,启动时不会静默覆盖用户密码。"
                )
    finally:
        db.close()

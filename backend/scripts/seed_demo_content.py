"""显式写入合成演示内容。

用法(后端容器内或已配置数据库环境的主机):
    python scripts/seed_demo_content.py                 # 写入/补齐演示内容(幂等)
    python scripts/seed_demo_content.py --list          # 只列出场景,不写数据库
    python scripts/seed_demo_content.py --output demo-seed.json

Compose 等价命令:
    docker compose exec backend python scripts/seed_demo_content.py

环境要求:
- OPSSOP_ENV 必须不是 production(production 明确拒绝);
- 账号 operator/reviewer 必须已由启动种子或账号管理创建,本命令不创建/修改账号;
- 演示内容仅用于演示与状态过滤验证,不构成独立评测集;不会清理任何既有数据。
"""

import argparse
import json
import os
import sys

# 允许 `python scripts/xxx.py` 直接运行:把应用根目录(/srv/opssop)加入导入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description="显式写入合成演示内容(非 production)")
    parser.add_argument(
        "--operator",
        default="operator",
        help="演示内容的运维账号用户名(默认 operator;账号须已存在)",
    )
    parser.add_argument(
        "--reviewer",
        default="reviewer",
        help="演示内容的审核账号用户名(默认 reviewer;账号须已存在)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出场景标识,不连接数据库、不写入任何内容",
    )
    parser.add_argument(
        "--output",
        default="",
        help="可选:把运行摘要写入指定 JSON 文件(不包含密钥)",
    )
    args = parser.parse_args()

    from app.config import get_settings
    from app.demo_content import (
        ALL_SCENARIOS,
        DemoSeedError,
        DemoSeedForbidden,
        seed_demo_content,
    )

    if args.list:
        payload = {
            "scenarios": [{"key": s.key, "title": s.title} for s in ALL_SCENARIOS],
            "note": "仅列出定义,未连接数据库、未写入任何内容。",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    settings = get_settings()
    if settings.is_production:
        print(
            "拒绝执行:production 环境禁止写入演示内容。"
            "请在隔离的开发/测试环境显式运行本命令。",
            file=sys.stderr,
        )
        return 2

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        summary = seed_demo_content(
            db,
            operator_username=args.operator,
            reviewer_username=args.reviewer,
            settings=settings,
        )
    except DemoSeedForbidden as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 2
    except DemoSeedError as exc:
        print(f"演示内容初始化失败: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 输出安全摘要,不回显堆栈/配置
        print(f"演示内容初始化失败(已回滚): {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        db.close()

    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        out_path = os.path.abspath(args.output)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"运行摘要已写入: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

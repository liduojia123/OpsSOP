# 本地开发

适用版本：3.7.2。无需 GPU 或模型服务，`AI_MODE=disabled` 即可开发完整业务闭环。

## 环境

- Python 3.12、Node.js 24
- Docker（只用于运行 PostgreSQL 16 + pgvector）
- Windows 使用 Git Bash / PowerShell 均可；WSL 内执行 Docker 命令

## 1. 启动数据库

```bash
cp .env.example .env      # 修改 POSTGRES_PASSWORD、SECRET_KEY、OPSSOP_ADMIN_PASSWORD
docker compose up -d db
```

数据库监听 `127.0.0.1:${POSTGRES_PORT:-5432}`，账号见 `.env`。

## 2. 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
source .venv/Scripts/activate      # Windows Git Bash
pip install -r requirements.txt
```

创建 `backend/.env`（已被忽略），至少包含：

```text
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=opssop
POSTGRES_PASSWORD=change-me-strong-password
POSTGRES_DB=opssop
POSTGRES_TEST_DB=opssop_test
SECRET_KEY=dev-only-secret
OPSSOP_ENV=development
AI_MODE=disabled
EMBEDDING_MODE=disabled
```

`app/config.py` 读取**当前工作目录**下的 `.env`，因此命令都在 `backend/` 内执行。根目录的 `.env` 供 Compose 启动数据库使用，`backend/.env` 供本机后端进程使用，数据库账号与端口需保持一致。

```bash
alembic upgrade head
python -c "from app.seed import ensure_seed; ensure_seed()"
uvicorn app.main:app --reload --port 8000
```

- API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：`/api/system/health`
- 首次启动自动创建管理员账号（默认 `admin`，口令见 `OPSSOP_ADMIN_PASSWORD`）

## 3. 前端

```bash
cd frontend
npm ci
npm run dev
```

<http://127.0.0.1:5173>，`/api` 代理到 `127.0.0.1:8000`；后端端口不同时用 `BACKEND_URL=http://127.0.0.1:<port> npm run dev`。

## 4. 测试

统一入口（推荐，需要 Docker；与 CI 使用相同镜像与命令）：

```bash
python scripts/check.py all        # 或 frontend / backend 单独执行
```

脚本使用临时环境文件和独立 Compose 项目，不读取根目录 `.env`，结束后清理容器与数据卷。

本机迭代时也可以直接运行：

```bash
cd frontend && npm test && npm run build     # build 含 vue-tsc 类型检查
cd backend && POSTGRES_HOST=127.0.0.1 pytest tests/ -q
```

后端测试必须使用独立测试库，`POSTGRES_TEST_DB` 与 `POSTGRES_DB` 相同会直接拒绝运行；首次运行会自动创建测试库。`tests/conftest.py` 在容器内默认 `POSTGRES_HOST=db`，本机运行必须显式覆盖为 `127.0.0.1`。Windows 宿主直接 `npm run build` 可能遇到 esbuild 临时文件占用，此时使用上面的 Docker 入口。

## 5. 常用命令

| 用途 | 命令 |
|---|---|
| 全部检查 | `python scripts/check.py all` |
| 数据库迁移 | `cd backend && alembic upgrade head` |
| 新增迁移 | `cd backend && alembic revision -m "..." --autogenerate` |
| 演示数据 | `cd backend && python scripts/seed_demo_content.py` |
| 前端单测 | `cd frontend && npm test` |
| 后端单测 | `cd backend && POSTGRES_HOST=127.0.0.1 pytest tests/ -q` |
| 检索/AI 评测 | 见[质量与评测](docs/evaluation.md) |

## 6. 代码结构

```text
backend/app/          API、业务、模型适配、检索、评测与运维
backend/alembic/      数据库迁移（只新增 revision，不改已发布版本）
backend/tests/        后端测试
frontend/src/         页面、组件、样式与 API 客户端
frontend/tests/       组件测试
docs/                 架构、部署、检索、对话、备份等文档
scripts/              端到端与视觉检查脚本（指向独立环境）
```

## 7. 改动约定

- 权限、审核状态与版本检查在后端执行，前端不作为信任边界。
- 数据库结构变化必须新增 Alembic 迁移，并附带升级说明。
- AI 输出按固定 Schema 解析，证据与引用由后端校验；用户日志和知识内容视为不可信输入。
- 版本号变更需同步 `VERSION`、`frontend/package.json` 与锁文件、后端 API 版本、Compose 镜像引用。
- 提交前运行相关测试并在 PR 中写明实际结果；未执行就写明未执行。

完整贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。
# OpsSOP

运维故障知识库与 SOP 管理系统：提交故障和日志 → 人工补充检查 → 确认根因并生成案例 → 审核案例 → 生成并发布 SOP → 检索复用。

技术栈：Vue 3、TypeScript、Vite、Element Plus、Tailwind CSS / FastAPI、SQLAlchemy、Alembic / PostgreSQL / Docker Compose。

项目处于早期开发阶段，基础流程已实现，仍有下文列出的已知问题。AI 助手、模型调用、关键词检索和向量检索尚未实现；当前从案例生成 SOP 是人工触发的模板复制流程。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1. 基础业务 | 登录权限、故障处理、案例审核、SOP 审核与版本、迁移和 Compose | 已实现主要流程，尚未完全验收 |
| 2. AI 助手 | 模型适配、关键词检索、右侧助手、防抖与过期响应控制 | 未开始 |
| 3. 沉淀检索 | AI 辅助 SOP 生成、embedding、混合检索、索引管理 | 未开始 |
| 4. 验证交付 | 扩展端到端验证、评测及部署文档 | 未完成 |

## 本地启动

需要 Docker Compose v2。Windows 使用 Docker Desktop（WSL2 后端）；普通 Linux 服务器部署尚未实测。前端、后端和数据库运行不需要 GPU。

```bash
git clone https://github.com/liduojia123/OpsSOP.git
cd OpsSOP
cp .env.example .env
```

PowerShell 可用 `Copy-Item .env.example .env`。编辑 `.env`，至少替换 `POSTGRES_PASSWORD`、`SECRET_KEY`、`OPSSOP_ADMIN_PASSWORD`。真实 `.env`、备份和业务数据不要提交到仓库。

```bash
docker compose up --build -d
```

- 前端：<http://localhost:8080>
- API 文档：<http://localhost:8000/docs>
- 健康状态：<http://localhost:8000/api/system/health>

首次启动执行迁移、分类和账号初始化。管理员账号由 `OPSSOP_ADMIN_USERNAME` / `OPSSOP_ADMIN_PASSWORD` 配置；`OPSSOP_SEED_DEMO=1` 时创建 `operator`、`reviewer` 演示账号，密码来自对应环境变量。

Compose 中数据库和后端端口绑定 `127.0.0.1`，前端默认绑定所有网络接口。公开部署前需自行配置访问入口、HTTPS 和正式账号。

## 权限与业务流程

权限由后端执行：运维人员处理自己的故障及案例；审核人员查看并审核材料；管理员管理账号、分类和业务内容。正式知识来自已通过案例和有效已发布 SOP。

```text
故障：草稿 ↔ 处理中 → 人工确认解决
                       └─ 案例：待审核 → 通过 / 退回后修改重提
                                         └─ SOP 草稿 → 提交 → 发布 / 退回
                                               └─ 新版本草稿 → 审核发布
                                                    └─ 旧发布版本转历史
```

人工确认根因、处理步骤和恢复验证为必填内容。原始输入、人工确认、案例及 SOP 分开保存，重要状态变更记录审计。新版本发布前，旧发布版本保持有效；草稿和未审核内容按后端权限过滤。

## 已知问题

截至 2026-10-14 的独立复核，仍有以下遗漏：

1. 切换故障状态会覆盖未保存的描述、日志等输入。
2. 请求进行期间继续输入，成功响应可能覆盖或清空新输入，涉及故障记录和 SOP 提交等场景。
3. SOP 多步提交期间切换历史版本，后续请求可能使用变化后的版本 ID，导致原版本提交失败。
4. `prepare_production.py --disable` 的参数解析与遍历逻辑不一致，不能可靠停用目标账号。
5. 对已有案例的历史异常故障执行 `repair_fake_resolved.py --apply` 后，再次确认解决可能因案例唯一约束返回 500。

这些缺陷需补相应回归后修复。一个单元测试还依赖默认测试库名 `opssop_test`；浏览器脚本中的审核员按钮断言尚未覆盖未解决故障。

## 测试与验证

```bash
# 已配置 .env，并构建后端镜像；测试使用独立测试库
docker compose up -d db
docker compose run --rm --no-deps backend python -m pytest tests/ -q

# 前端类型检查和生产构建；本地验证使用 Node.js 24 / npm 11
cd frontend
npm ci
npm run build
```

`POSTGRES_TEST_DB` 必须与业务库 `POSTGRES_DB` 不同，默认是 `opssop_test`。迁移测试使用随机一次性数据库。测试会清理测试库中的表，请使用专门的测试数据库。

`scripts/e2e_loop.py` 提供 API 业务闭环，使用 Python 标准库；`scripts/browser_e2e.py` 另需 Playwright Python 包和系统 Google Chrome。两个脚本均会写入测试数据，应指向独立测试环境，并通过 `E2E_*` 环境变量显式提供地址及凭据；变量列表见脚本开头。

2026-10-14 独立复核结果：

- 默认测试库配置下，后端 **52 项测试通过**。
- 临时 PostgreSQL 16 中空库迁移、升级/回滚/再升级通过，包含特殊字符密码的连接路径。
- 前端类型检查和生产构建通过。
- 两个维护脚本缺陷在临时 PostgreSQL 中复现；前端输入及路由问题由实际 Vue 逻辑的 HTTP mock 探针复现。
- 自定义测试库名时，一个写死库名的单元测试断言失败。

此次未独立重跑浏览器完整闭环；Linux 云部署、真实模型接口、检索和备份恢复尚未验证。测试通过仅代表现有覆盖范围，不代表已知问题已经修复。公开快照只调整文档和发布元数据，没有改动业务实现。

## 配置与模型计划

当前建议保持 `AI_MODE=disabled`、`EMBEDDING_MODE=disabled`。`demo`、`cloud`、`local` 模式目前只有配置及状态接口预留，不代表已有模型调用能力。

后续计划通过后端适配层支持 OpenAI 兼容接口，聊天和 embedding 分别配置；端点、模型、可选密钥及超时由环境变量管理。未配置 embedding 时计划提供关键词检索。

已实现 `LOCAL_ONLY` 与 cloud 配置冲突检查；完整的模型外发控制需随调用链实现并验证。云模型模式届时会向配置的服务发送脱敏输入及检索片段。AI 候选原因与人工确认事实分别保存，AI 不自动执行运维命令、确认根因或发布 SOP。

Windows 容器中的 `localhost` 指向容器自身；未来访问宿主机模型服务时可配置 `host.docker.internal`。应用部署位置与模型服务位置独立。

## 生产账号与维护

`OPSSOP_ENV=production` 会拒绝示例默认口令、启用演示种子以及库内遗留演示账号。环境变量只用于首次初始化，不会覆盖已有账号密码。账号修改可通过系统管理界面完成。

数据库已启动、后端镜像已构建时，可以运行下列只读检查，即使后端因生产检查失败而未运行：

```bash
docker compose run --rm --no-deps backend python scripts/prepare_production.py
docker compose run --rm --no-deps backend python scripts/repair_fake_resolved.py
```

以上命令不带修改参数，只报告问题。`--disable` 和历史故障修复的 `--apply` 有已知缺陷，不应作为已验证的生产整改路径。

## 数据与迁移

业务数据存于 Docker 卷 `pgdata`，应用不会自动同步到其他存储。

```bash
# 将占位参数替换为自己的配置
docker compose exec -T db pg_dump -U <POSTGRES_USER> <POSTGRES_DB> > backup.sql
cat backup.sql | docker compose exec -T db psql -U <POSTGRES_USER> <POSTGRES_DB>

# 迁移也会在正常后端启动时执行
docker compose run --rm --no-deps backend alembic upgrade head
```

备份恢复命令需按实际 shell 和编码检查，本项目尚未完成恢复演练。

## 目录

```text
backend/app/       FastAPI 业务与权限
backend/alembic/   数据库迁移
backend/scripts/   启动及维护工具
backend/tests/     后端回归测试
frontend/src/      Vue 工作台
scripts/           API 与浏览器闭环脚本
```

## 许可证

项目原创代码采用 [MIT License](LICENSE)。依赖和基础镜像保留各自许可证，见 [第三方依赖说明](THIRD_PARTY_NOTICES.md)。

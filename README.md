# OpsSOP

**让每一次故障处理，成为团队下一次可以复用的标准操作。**

OpsSOP 是面向运维团队的故障知识库与 SOP 管理系统，将问题记录、证据补充、人工确认、案例审核和 SOP 发布连接起来，并提供可选的 AI 排查助手与知识检索。

[产品使用](docs/product-guide.md) · [部署指南](docs/deployment.md) · [模型配置](docs/ai-configuration.md) · [运维手册](docs/operations.md) · [版本记录](CHANGELOG.md)

当前版本 **1.1.2**：密码重置后撤销旧会话，并增加登录限流与失败审计。升级说明见[版本记录](CHANGELOG.md)，视觉与动效范围见[界面说明](docs/ui-redesign.md)。

## 产品能力

| 能力 | 说明 |
|---|---|
| 故障处理 | 记录环境、服务、日志、近期变更和处理过程，保存原始证据与人工确认结果 |
| AI 排查助手 | 整理事实、推荐分类、提供检查建议，展示证据与知识来源；支持自动分析和手动触发 |
| 案例沉淀 | 人工确认实际根因、处理方法与恢复结果，审核后进入正式案例库 |
| SOP 管理 | 手工编写、模板生成或 AI 辅助起草，支持审核、发布、历史版本、废弃与反馈 |
| 知识检索 | 中文关键词检索独立可用，可接入 embedding 与 pgvector 混合检索 |
| 权限与审计 | 运维、审核、管理员三类角色，后端权限控制，关键状态变更留痕 |
| 自托管交付 | Docker Compose 部署、数据卷持久化、数据库迁移、备份恢复及隔离运维演练 |

```text
记录故障与日志 → 补充证据 → 人工确认解决 → 案例审核
                                             ↓
知识检索与排查复用 ← SOP 审核发布 ← 起草与人工补全
```

AI 建议与人工确认结果分别保存。AI 不执行运维命令，不自动解决故障，不自动发布 SOP。

## 快速启动

需要 Docker Engine 与 Docker Compose。Windows 可使用 Docker Desktop 或 WSL2 内的 Docker；WSL2 场景在发行版内执行 Docker 命令。生产部署使用 Compose **2.24.4 或更高版本**。

```text
git clone https://github.com/liduojia123/OpsSOP.git
cd OpsSOP
git checkout v1.1.2
```

复制环境变量示例：

```powershell
# PowerShell 7
Copy-Item .env.example .env
```

```sh
# Linux / WSL2
cp .env.example .env
```

编辑 `.env`，至少替换 `POSTGRES_PASSWORD`、`SECRET_KEY`、`OPSSOP_ADMIN_PASSWORD`。`SECRET_KEY` 使用随机长字符串。首次启动创建管理员，日常账号由管理员在系统管理中创建。

```text
docker compose -p opssop-local --env-file .env up --build -d
```

打开 [http://localhost:8080](http://localhost:8080)，使用配置的管理员账号登录。本地 API 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)。首次启动自动执行迁移和基础分类初始化。

默认关闭 AI 与 embedding，人工业务和关键词检索可以独立使用。需要模拟场景时，在独立环境显式启用 `OPSSOP_SEED_DEMO=1` 与 `AI_MODE=demo`，详见[产品使用指南](docs/product-guide.md)。

账号安全：密码重置后已有会话失效；升级到包含该功能的版本时，旧会话需要重新登录。登录次数限制及反向代理部署注意事项见[管理入口](docs/product-guide.md#管理入口)。

## 生产部署

生产使用独立环境文件、固定发行版本和生产覆盖配置：

```text
docker compose -p opssop-prod --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

`.env.production` 从 `.env.production.example` 复制并填写实际配置。生产配置禁用演示账号，数据库与后端不发布宿主端口；前端仅监听 `127.0.0.1`，通过宿主反向代理提供 HTTPS。完整步骤、账号初始化、升级与回退见[部署指南](docs/deployment.md)。

数据库保存在部署所在地的命名卷中。升级前备份，恢复到新库后再切换应用；不要删除正在使用的数据卷。[备份恢复手册](docs/backup-restore.md)提供操作流程。

## 模型与数据

聊天模型和 embedding 分别配置，支持 OpenAI 兼容接口；应用与数据库不需要 GPU。

| 模式 | 使用方式 |
|---|---|
| `disabled` | 关闭模型调用，保留人工业务与关键词检索 |
| `demo` | 显式模拟聊天分析，结果标记为演示；embedding 无此模式 |
| `cloud` | 调用管理员配置的云模型接口 |
| `local` | 调用管理员配置的本机或内网模型接口 |

云模式会向配置的服务发送经过服务端脱敏的输入及相关知识片段。`LOCAL_ONLY=1` 禁止聊天和 embedding 使用云配置；严格出口控制由部署环境承担。真实接口失败会返回明确错误，embedding 不可用时检索明确降级为关键词方式。

模型接入、用量限制、索引构建及网络要求见[模型配置指南](docs/ai-configuration.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [产品使用](docs/product-guide.md) | 角色、工作台、故障处理、案例审核、SOP 发布与检索 |
| [部署指南](docs/deployment.md) | 环境准备、生产配置、HTTPS 入口、升级与回退 |
| [模型配置](docs/ai-configuration.md) | 聊天、embedding、local-only、索引与故障处理 |
| [系统架构](docs/architecture.md) | 组件、数据流、状态流转与安全边界 |
| [运维手册](docs/operations.md) | 日常巡检、隔离演练、日志与故障处理 |
| [备份恢复](docs/backup-restore.md) | 备份、恢复到新库、数据切换与保留 |
| [质量与评测](docs/evaluation.md) | 自动化工具、指标定义、既有结果及适用范围 |
| [界面与动效](docs/ui-redesign.md) | 视觉分层、交互动效、状态覆盖与本次检查结果 |
| [版本交付](docs/releasing.md) | 发行、部署交接与维护流程 |

## 技术栈与源码

Vue 3、TypeScript、Vite、Element Plus、Tailwind CSS；FastAPI、SQLAlchemy、Alembic；PostgreSQL 16、pgvector；nginx、Docker Compose。

```text
backend/app/          API、业务、模型适配、检索、评测与运维模块
backend/alembic/      数据库迁移
backend/scripts/      初始化、索引、评测与运维命令
backend/eval_data/    版本化合成评测集
frontend/src/         工作台与业务页面
backend/tests/       后端自动化测试
frontend/tests/      前端组件测试
scripts/             API 与浏览器业务闭环工具
docs/                产品、部署与维护文档
```

本发行以单组织、单应用实例的自托管方式使用；不包含多租户计费、订阅支付或托管 SLA。模型输出需要人工判断，语义检索是否有收益取决于语料和模型；已有评测范围见[质量与评测](docs/evaluation.md)。

## 参与与许可

问题和功能建议请提交 [GitHub Issues](https://github.com/liduojia123/OpsSOP/issues)，提交前删除凭据与业务隐私。参与维护见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

本项目采用 [MIT License](LICENSE)，允许在遵守许可条款的前提下商用、修改和分发。第三方依赖保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

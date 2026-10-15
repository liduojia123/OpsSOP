# OpsSOP

运维故障知识库与 SOP 管理系统：录入问题和日志 → AI 辅助排查 → 人工补证与确认根因 → 案例审核 → SOP 发布 → 检索复用。

技术栈：Vue 3、TypeScript、Vite、Element Plus、Tailwind CSS / FastAPI、SQLAlchemy、Alembic / PostgreSQL / Docker Compose。前端、后端和数据库无需 GPU，模型服务独立配置。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1. 基础业务 | 登录权限、故障处理、案例审核、SOP 版本与审计 | 已实现，修复包含在后端回归中 |
| 2. AI 助手 | 四种模式、关键词检索、右侧助手、结构化报告与引用校验 | 已实现并独立验收通过；验证范围见下文 |
| 3. 沉淀检索 | AI 辅助 SOP 生成、embedding、混合检索、索引管理 | 待开发，按三批交付；第一批计划见下文 |
| 4. 验证交付 | 扩展浏览器闭环、真实模型评测、部署与恢复演练 | 未完成 |

当前 SOP 草稿由人员从已审核案例主动生成，使用模板复制；尚未实现 AI 编写 SOP 或向量检索。

第三阶段按三批交付：AI 辅助生成 SOP 草稿 → embedding 与索引管理 → 混合检索及权限、失效、降级验证。[第一批开发任务与验收计划](docs/phase-3-batch-1-ai-sop-draft.md)已入库，状态为待实现；每批复审通过后再进入下一批。

## 本地启动

需要 Docker Compose v2。Windows 使用 Docker Desktop（WSL2 后端）；Linux 适配仍待实际部署验证。

```bash
git clone https://github.com/liduojia123/OpsSOP.git
cd OpsSOP
cp .env.example .env
```

PowerShell 中复制命令为 `Copy-Item .env.example .env`。编辑 `.env`，至少更换 `POSTGRES_PASSWORD`、`SECRET_KEY`、`OPSSOP_ADMIN_PASSWORD`；使用演示账号时也更换其密码。

```bash
docker compose up --build -d
docker compose ps
```

- 前端：[http://localhost:8080](http://localhost:8080)
- API 文档：[http://localhost:8000/docs](http://localhost:8000/docs)
- 健康检查：[http://localhost:8000/api/system/health](http://localhost:8000/api/system/health)

首次启动执行迁移、分类与账号初始化。管理员用户名由 `OPSSOP_ADMIN_USERNAME` 配置，密码来自 `OPSSOP_ADMIN_PASSWORD`；`OPSSOP_SEED_DEMO=1` 时创建 `operator`、`reviewer`，密码分别来自对应的 `OPSSOP_DEMO_*_PASSWORD`。

修改 `.env` 后用 `docker compose up -d backend` 重建容器配置；修改源码后用 `docker compose up -d --build backend frontend`。单纯 `restart` 不会载入新的 Compose 环境变量。

## 权限与业务流程

后端执行权限检查：运维人员处理自己的故障和案例；审核人员查看并审核材料；管理员管理账号、分类及业务内容。正式诊断知识只使用当前用户有权访问的已通过案例和有效已发布 SOP。

```text
故障草稿 ↔ 处理中 → 人工确认解决
                     └─ 案例待审核 → 通过 / 退回重提
                                      └─ SOP 草稿 → 提交 → 发布 / 退回
                                           └─ 新版本草稿 → 审核发布
                                                └─ 旧版转历史
```

人工确认根因、处理步骤和恢复验证为必填内容。原始输入、AI 候选原因和人工确认分别保存，重要状态变更留审计；新 SOP 版本发布前，旧发布版本继续有效，废弃或历史版本不作为当前正式知识。

第一阶段的输入覆盖、在途操作与历史版本切换、生产账号维护脚本、历史异常故障修复、测试库名写死等问题已修复并加入回归。

## 模型配置与数据流

| `AI_MODE` | 行为 |
|---|---|
| `disabled` | 默认；不调用模型，人工业务和关键词检索仍可用 |
| `demo` | 本地模拟分析，明确标注“演示数据 / 模拟分析”，用量未知 |
| `cloud` | 调用配置的云端 OpenAI 兼容 `chat/completions` 接口 |
| `local` | 调用配置的本机或内网兼容接口，不继承环境 HTTP 代理 |

真实接口失败会返回错误，不会自动切换到 demo 或其他模型。最小云端配置示例（填入自己的服务信息）：

```dotenv
AI_MODE=cloud
AI_BASE_URL=https://your-model-service.example/v1
AI_MODEL=your-model-name
AI_API_KEY=replace-with-your-key
AI_SUPPORTS_JSON_MODE=0
AI_SUPPORTS_USAGE=0
LOCAL_ONLY=0
EMBEDDING_MODE=disabled
```

`AI_BASE_URL` 填接口前缀，适配层追加 `/chat/completions`。仅当服务支持时开启 `AI_SUPPORTS_JSON_MODE=1`；结构化分析请求按需使用 JSON 模式，普通请求不强制携带 `response_format`。完整配置见 [.env.example](.env.example)。

云模式的数据流：问题、日志、检查记录及检索片段 → 后端脱敏与长度限制 → 配置的外部模型 → Schema、证据 ID 和引用校验 → 分析记录。数据库保存在应用部署地；脱敏不代表所有敏感信息都可识别。

密钥只由后端读取，放在被 Git 忽略的 `.env`，不得提交或加入前端。`LOCAL_ONLY=1` 禁止聊天和 embedding 使用 cloud 配置；local 端点限制为受信任本机/内网目标，失败不外切。严格的出口隔离还需部署环境限制网络。

聊天与 embedding 独立配置；当前 embedding 功能未实现，保持 `EMBEDDING_MODE=disabled`。Windows 容器访问宿主机模型可使用 `host.docker.internal`；容器里的 `localhost` 指向容器自身。

当前没有思考模式/`reasoning_effort` 的配置开关。启用服务默认推理能力时，应实测耗时与输出额度是否足够；不要添加应用不识别的环境变量。

## 右侧排查助手与知识检索

- 输入停顿约 2 秒自动分析，可关闭并手动触发；空输入或无实质变化不调用。
- 绑定故障、输入快照和模型配置版本；旧分析、历史或搜索响应不能覆盖新状态。卸载、切换故障和关闭自动分析使待排队请求失效。
- 单用户限流和并发控制；超时、429、非法输出不清空输入，不循环调用。JSON 语法与 Schema 结构错误共享一次格式修复预算，反馈字段路径和期望类型；一次分析最多产生两次模型调用。
- 缓存绑定故障与用户，检查 TTL；返回缓存、历史、搜索结果及模型报告时复核来源权限、状态和版本。
- 固定报告包含摘要、有证据绑定的事实、候选分类/原因、待补信息、下一步检查和相关知识；无效引用剔除并标注不完整，允许证据不足。
- 业务失败与失败历史持久展示原因、失败类型和重试入口；状态、调用次数、延迟及用量独立于报告展示。请求失败保留旧成功报告时标明旧结果。
- 用量来自服务实际返回值，多次调用按字段完整性汇总；缺失显示未知，不估算费用。AI 命令只展示和复制，不自动执行、确认根因或发布 SOP。
- 关键词检索独立于模型，支持中文词片段匹配；只返回有效正式知识，无匹配返回空结果。

首次真实调用建议关闭自动分析，用虚构问题和仅含演示材料的知识库手动测试，避免检索到实际业务片段。

## 测试

后端测试使用独立 PostgreSQL 测试库；`POSTGRES_TEST_DB` 必须与 `POSTGRES_DB` 不同，默认 `opssop_test`。测试会清表，数据库账号需有创建测试库权限。迁移测试另建随机一次性数据库。

```bash
docker compose up -d db
docker compose build backend
docker compose run --rm --no-deps -e AI_MODE=disabled -e EMBEDDING_MODE=disabled -e OPSSOP_ENV=development -e OPSSOP_SEED_DEMO=1 backend python -m pytest tests/ -q
```

前端验证使用 Node.js 24 / npm 11；组件测试为 Vitest、Vue Test Utils、happy-dom，mock API，不依赖后端：

```bash
cd frontend
npm ci
npm test
npm run build
```

`scripts/e2e_loop.py` 为标准库 API 闭环；`scripts/browser_e2e.py` 另需 Playwright Python 包与系统 Google Chrome。两个脚本均写测试数据，需指向独立环境，并按脚本开头显式设置 `E2E_BASE_URL` 及各角色 `E2E_*_PASSWORD`，不要指向生产库。

### 真实接口冒烟（会产生模型调用）

先在 `.env` 配置 `AI_MODE=cloud|local`、目标接口和可选密钥，再创建/更新后端容器。脚本只验证适配层，不写数据库；demo/disabled 会拒绝运行。

Bash：

```bash
docker compose exec -T backend python - < backend/scripts/smoke_llm.py
```

PowerShell（兼容 Windows PowerShell 的 UTF-8 管道）：

```powershell
$opssopOldOutputEncoding = $OutputEncoding
try {
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    Get-Content -Raw -Encoding UTF8 backend/scripts/smoke_llm.py | docker compose exec -T backend python -
} finally {
    $OutputEncoding = $opssopOldOutputEncoding
}
```

JSON 能力关闭时，该项记“跳过 / 未验证”；开启时必须返回裸 JSON 对象，数组、围栏和杂文不算通过。退出码 0 可能包含跳过项，应检查逐项状态。`usage` 缺失时显示未返回。

## 验证范围（2026-10-15）

在 Windows Docker Desktop 上使用只读源码快照、Linux 容器和独立临时 PostgreSQL 数据库完成独立复审，没有读写原业务库。

- 后端 **120 项 pytest 通过（143.55 秒）**；前端 **19 项 Vitest 通过**，`vue-tsc -b && vite build` 通过。覆盖权限、状态、迁移、脱敏、并发检索、有限修复预算、引用版本和失败展示。构建仍有主包超过 500 kB 的提示。
- 7 项无网络补充探针通过：`version:"v2"` 修复为整数 2、错误版本引用剔除、语法/结构错误共享一次预算、缺失 usage 和修复超时保持未知。
- Chromium 浏览器：演示业务闭环 **42 项检查通过**；真实云端补证 **20 项检查通过**；使用明确标注的脚本适配器、真实 API 和持久化记录完成失败展示 **12 项检查**，其中用量是合成夹具值，不是真实模型消耗。
- DeepSeek `deepseek-flash` 完整分析生成两份 `success` 报告。第一份在首次输出达到额度、提取对象不符合 Schema 后，经唯一一次修复成功；第二份由浏览器追加证据和未保存描述后手动触发，一次调用成功。所有请求使用 JSON 模式，来源和证据 ID 经过复核。
- 同输入缓存复用通过，没有新增模型请求；真实报告已引用的 SOP v2 升版后，历史回读剔除旧引用并解释失效，数据库原始报告保持不变。草稿、生效、历史和废弃状态过滤及审计通过。
- 本轮实际上游请求 **3 次**，usage 合计输入 3,866、输出 7,828、总计 11,694 tokens；推理消耗已包含在输出中，不重复相加，不估算费用。模型请求只含纯合成测试数据。
- `version:"v2"` 的精确修复路径由合成适配器验证；本轮真实模型触发的是截断后 Schema 错误，不能声称真实模型重现了同一版本字符串错误。
- 尚未验证：真实 local 模型、其他云服务、普通 Linux 云服务器部署、备份恢复演练和模型效果评测。第三阶段未启动，embedding、混合检索及 AI 编写 SOP 未实现。

此前完成的 28 次合成凭据请求边界探针、12 项 Vue 生命周期和真实 Element Plus 输入事件探针仍作为历史证据，本轮没有将它们重复计为新增检查。

## 生产账号与部署

Compose 的数据库和 API 端口绑定 `127.0.0.1`，前端默认绑定所有网络接口。公开部署需配置 HTTPS、访问入口与正式账号；项目不会自动部署公网服务。

`OPSSOP_ENV=production` 拒绝示例口令、演示种子及库中遗留演示账号。环境变量仅用于首次初始化，不覆盖已有口令；已有账号通过系统管理或维护脚本显式修改。先用只读报告检查：

```bash
docker compose run --rm --no-deps backend python scripts/prepare_production.py
docker compose run --rm --no-deps backend python scripts/repair_fake_resolved.py
```

账号脚本支持显式 `--disable <用户名>` / `--rotate <用户名> <新口令>`；历史异常故障脚本的 `--apply` 会修改数据。执行修改前备份，并在完成后复检、重启后端。

## 数据、备份与迁移

业务数据保存在 Docker 卷 `pgdata`，不自动同步到其他存储。以下备份避免通过 shell 文本重定向传输数据库内容；先建立 `backups` 目录（Bash：`mkdir -p backups`；PowerShell：`New-Item -ItemType Directory -Force backups`）。该目录已被 Git 忽略。命令中的 `-U opssop`、备份的 `-d opssop` 使用示例账号与库名，修改过配置时替换为自己的值。

```bash
docker compose exec -T db pg_dump -U opssop -d opssop -Fc -f /tmp/opssop-backup.dump
docker compose cp db:/tmp/opssop-backup.dump ./backups/opssop-backup.dump
```

恢复示例写入一个新库 `opssop_restore`，不会切换应用连接；该库名须尚不存在。恢复后应检查数据并单独验证应用，再自行决定如何切换。

```bash
docker compose cp ./backups/opssop-backup.dump db:/tmp/opssop-restore.dump
docker compose exec -T db createdb -U opssop opssop_restore
docker compose exec -T db pg_restore --exit-on-error --no-owner -U opssop -d opssop_restore /tmp/opssop-restore.dump
```

上述 Compose 命令可用于 Bash 和 PowerShell；备份恢复流程尚未演练。备份含业务数据，应自行控制保存位置与访问权限，勿提交到仓库。

迁移随正常启动执行，也可手动运行 `docker compose run --rm --no-deps backend alembic upgrade head`。保留 `pgdata` 卷；`docker compose down -v` 会删除其中的数据。

## 目录与许可证

`backend/app/` 为业务、模型及检索；`backend/alembic/` 为迁移；`backend/scripts/` 为启动和维护；`backend/tests/` 与 `frontend/tests/` 为回归；`frontend/src/` 为 Vue 工作台；`scripts/` 为业务闭环验证。

原创代码采用 [MIT License](LICENSE)。第三方依赖和基础镜像保留各自许可证，见 [第三方依赖说明](THIRD_PARTY_NOTICES.md)。

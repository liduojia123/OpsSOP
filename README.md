# OpsSOP — 运维故障知识库与 SOP 管理系统

个人作品展示 / 面试演示向的运维故障知识库与 SOP 管理系统:提交问题与日志 → 人工补充检查 → 确认根因生成案例 → 审核 → 生成/发布 SOP → 检索复用。

**技术栈**:Vue 3 + TypeScript + Vite + Tailwind CSS + Element Plus / FastAPI + SQLAlchemy + Alembic + PostgreSQL / Docker Compose。

## 当前状态(随实施更新)

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1. 基础业务 | 登录权限、故障录入、人工分类、案例审核、SOP 状态与版本、迁移和 Compose | 已完成并通过质量修复验收 |
| 2. AI 助手 | 模型适配、配置模式、关键词检索、左右分栏、分类及分析、防抖与过期响应控制、错误处理和 demo | 后端+前端已实现;demo/协议已测试;cloud 已联调并完成 21 条真实评测(2026-10-27,阿里云 MaaS `deepseek-v4.1-flash`:分类 12/18、引用 133/133 有效、2 条失败),local 未验证;原 DeepSeek 配置的 key 已失效(401) |
| 3. 沉淀检索 | SOP 生成、embedding、混合检索、索引管理、local-only | 三批均已实现并通过独立复审:第一批(AI 生成 SOP 草稿)、第二批(embedding 适配与索引管理)、第三批(向量/混合检索与可靠性);已随第三阶段发布(PR #2,main `b7e56b5`)。真实 embedding 已联调(2026-10-27,`qwen3.7-text-embedding-flash`,1024 维),合成样本上混合检索未显示优于关键词 |
| 4. 验证交付 | 端到端演示、自动化测试、评测脚本、完整文档 | 第一批(来源定位、显式演示内容、demo 浏览器闭环)、第二批(版本化评测集、检索/分析评测、指标口径与机器可读报告)已通过独立验收;第三批(备份恢复、命名卷持久化、生产配置与操作文档)复审残留项已修复复验(2026-10-26);本地/本地 Linux 容器/合成协议范围通过,云端与真实模型效果未验证 |

已验证项目会在「验证记录」一节列出;**未经真实运行的项目一律标记为未验证**。

## 快速启动(本地 Docker Compose)

前提:Windows + Docker Desktop(WSL2 后端),或普通 Linux + Docker Compose v2。Windows 上直接使用 WSL2 发行版内的 Docker 引擎同样可行:命令在 WSL 内执行,项目目录从 `/mnt/c/...` 访问,构建、测试、演练都用 `docker compose` 的同一套配置。

```bash
# 1. 准备环境变量(复制示例,替换占位值)
cp .env.example .env
# 编辑 .env:至少设置 POSTGRES_PASSWORD、SECRET_KEY、OPSSOP_ADMIN_PASSWORD

# 2. 构建并启动(前端 + 后端 + PostgreSQL)
docker compose up --build -d

# 3. 访问
# 前端: http://localhost:8080
# 后端 API 文档: http://localhost:8000/docs
```

首次启动自动执行:数据库迁移(`alembic upgrade head`)→ 种子初始化(内置分类 + 管理员账号 + 可选演示账号)。

### 演示账号(OPSSOP_SEED_DEMO=1 时创建)

| 用户名 | 角色 | 密码来源 |
|---|---|---|
| `admin` | 管理员 | `OPSSOP_ADMIN_PASSWORD` |
| `operator` | 运维人员 | `OPSSOP_DEMO_OPERATOR_PASSWORD` |
| `reviewer` | 审核人员 | `OPSSOP_DEMO_REVIEWER_PASSWORD` |

`OPSSOP_ENV=production` 时的启动保护(实际行为):

- 拒绝示例默认口令/`OPSSOP_SEED_DEMO=1`,且在打开数据库前即失败;
- 启动时检测库内残留的默认口令账号、演示种子账号(含已换口令但用户名/显示名为演示指纹的账号),发现即**拒绝启动**并打印整改命令;
- 已有账号的口令**不会被环境变量静默覆盖**;换口令只走「系统管理」账号页或 `python backend/scripts/prepare_production.py --rotate <用户名> <新口令>`,停用走 `--disable <用户名>`,处理后需重启并复检。

历史数据中若有绕过人工确认的假「已解决」故障,可用 `python backend/scripts/repair_fake_resolved.py`(默认只报告)→ `--apply` 重置为处理中。

## 目录结构

```
├── docker-compose.yml      # 前端 + 后端 + PostgreSQL
├── .env.example            # 环境变量示例(无真实密钥)
├── backend/
│   ├── app/
│   │   ├── ai/             # 模型适配层、固定 Schema、提示词、脱敏、分析编排(第二阶段)
│   │   ├── search/         # 关键词检索(CJK 2-gram,正式知识过滤;向量检索属第三阶段)
│   │   ├── demo_content.py # 显式演示内容(稳定标识、幂等、production 拒绝;第四阶段第一批)
│   │   └── routers/        # auth/users/categories/faults/cases/sops/workbench/audit/system/ai
│   ├── alembic/            # 数据库迁移(含 demo_seed_records)
│   ├── scripts/            # start.sh / seed_demo_content.py / prepare_production.py / repair_fake_resolved.py
│   └── tests/              # pytest(权限/状态流转/迁移/生产保护/安全边界/AI/检索/演示 seed)
├── frontend/
│   ├── src/                # Vue3 + TS 工作台(登录/工作台/故障+AI 助手/案例/SOP/管理)
│   └── tests/              # vitest 组件行为测试(含来源定位、过期响应、登录回跳)
└── scripts/
    ├── e2e_loop.py         # API 级业务闭环(凭据/目标经环境变量,唯一运行标记)
    ├── browser_e2e.py      # 第一阶段浏览器完整闭环(Playwright + 系统 Chrome)
    └── demo_browser_e2e.py # 第四阶段 demo 浏览器闭环(JSON 结果/截图/失败注入)
```

## 角色与权限(后端强制)

| 能力 | 运维人员 operator | 审核人员 reviewer | 管理员 admin |
|---|---|---|---|
| 创建/处理自己的故障、追加记录、确认解决 | ✅ | ❌ | ✅ |
| 查看故障 | 自己的 | 全部 | 全部 |
| 提交/修改案例、生成 SOP 草稿 | ✅(自己的) | ❌ | ✅ |
| 审核案例(通过/退回)、审核/废弃 SOP | ❌ | ✅ | ✅ |
| 账号与分类管理 | ❌ | ❌ | ✅ |
| 读正式知识(已通过案例、已发布 SOP) | ✅ | ✅ | ✅ |

草稿与未审核材料仅创建者与审核权限可见;所有越权请求返回 403。

## 业务闭环与状态

```
故障(草稿 → 处理中 → 已解决)
  └─ 人工确认解决(实际根因/处理步骤/恢复验证,必填)→ 自动生成案例(待审核)
       └─ 审核:通过(正式诊断依据)或退回(可修改后重新提交)
            └─ 从已通过案例生成 SOP 草稿(人工主动触发,模板拷贝)
                 └─ SOP:草稿 → 提交 → 发布 / 退回
                      └─ 修改已发布 SOP → 新草稿版本(旧版本继续有效)
                           └─ 新版本发布时旧版本自动转为「历史版本」
                                └─ 废弃(不再作为有效知识)
```

原始证据、人工确认根因、案例、SOP 分表存储;AI 推测(第二阶段)不会进入已确认事实。

## AI 模型(第二阶段已接入)

| 模式 | 说明 | 状态 |
|---|---|---|
| `disabled` | 关闭 AI 分析,业务与关键词检索不受影响 | 当前默认,已测试 |
| `demo` | 明确标注的模拟分析,报告与结果块标注「演示数据 / 模拟分析」 | 已实现,已测试(模拟协议) |
| `cloud` | 调用用户配置的云模型 API(OpenAI 兼容 chat/completions) | 已实现;DeepSeek 云端接口真实冒烟通过(2026-10-14) |
| `local` | 调用本机/内网模型接口(Ollama、LM Studio、vLLM 等) | 已实现,目标校验已测试;真实联调未验证 |

配置全部通过环境变量(`AI_MODE`、`AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL`、`AI_SUPPORTS_JSON_MODE`、`AI_SUPPORTS_USAGE` 等),聊天与 embedding 独立配置;`AI_API_KEY` 只存在于被 Git 忽略的 `.env`,不进前端/日志/仓库。`LOCAL_ONLY=1` 时禁止 cloud 模式(应用级约束,严格隔离需部署环境限制出口)。

能力按接口显式声明:`AI_SUPPORTS_JSON_MODE` / `AI_SUPPORTS_USAGE` 不假设各服务行为一致;usage 仅在接口实际返回时记录,缺失显示「未知」,不估算费用。

**数据流提示**:cloud 模式下,服务端脱敏(口令/Token/私钥/JWT/AWS 密钥等)后的问题/日志片段与检索片段会发送至配置的外部模型接口;数据库仍保存在部署所在地。脱敏规则不能覆盖全部敏感信息,云模型调用不等于数据完全不出网。

### 右侧 AI 排查助手(故障详情页)

- 输入停顿约 2 秒自动分析(可关闭,手动「立即分析」);空输入或无实质变化不调用。
- 请求绑定故障 ID、输入内容指纹与模型配置版本;过期响应(序号落后/故障切换/输入已变)不覆盖新结果,仅标记过期;可取消旧请求;429 暂停自动分析,不循环请求。
- 同一输入 + 知识版本 + 模型配置版本复用缓存结果(复用前重新校验来源权限与状态);单用户限流与并发限制。
- 报告固定 Schema:摘要、已知事实(绑定证据 ID)、建议分类/标签(人工采纳可改)、候选原因(依据/待补信息/验证方法,「优先排查/次优先/证据不足」不做概率)、下一步检查(命令仅展示与复制,不执行)、相关案例与 SOP(引用 ID/版本经后端校验)。
- 模型失败落失败记录并可重试,不回退预设答案、不丢输入;分析不修改故障字段,AI 不自动解决故障或发布 SOP。
- 关键词知识检索独立可用(不依赖模型),只返回已审核案例与当前有效已发布 SOP。

### 来源定位(第四阶段第一批)

- 助手搜索命中与报告 `references` 均提供「查看案例 / 查看 SOP」入口;地址只由后端校验后的 `source_type`、正整数 `source_id` 与必要版本信息构造,不使用模型返回的任意 URL。
- 案例来源直达 `/cases/:id`(只读页,直接调用 `GET /api/cases/{id}`),不依赖案例列表筛选或缓存;SOP 继续使用 `/sops/:id`。可直接打开、刷新、收藏;同页切换记录时旧响应不会覆盖新记录。
- 来源链接在新标签页打开,不离开故障编辑页,因此左侧未保存的问题/日志输入不会丢失。
- 未登录直达来源时跳转登录并携带 `redirect`,登录后回到同一记录;`redirect` 仅允许站内绝对路径(拒绝 `//evil`、反斜杠与登录页自身)。
- 403、404、非法 ID、网络错误分别显示明确状态;失败或切换来源时不残留上一个来源正文。已审核案例可读,但页面只返回案例内容,不打开来源故障的原始日志、私有描述或生成历史。
- SOP 历史/废弃版本直达时保留「历史版本/已废弃」状态提示,不把旧版本悄悄替换为另一版本正文。

### AI 辅助生成 SOP 草稿(第三阶段第一批)

在案例详情(已通过案例)提供两个名称明确区分的生成入口,均为人工主动触发:

- **模板生成草稿**:既有 `POST /api/cases/{id}/generate-sop`,字段模板拷贝,不含模型调用。
- **AI 生成草稿**:新增 `POST /api/cases/{id}/generate-sop-ai`,调用当前配置模型整理来源材料生成结构化草稿,成功后创建新的 draft 并进入 SOP 编辑页,由人工核对、补充后走既有提交/退回/审核/发布流程;**不自动提交审核、不自动发布、不覆盖已有 SOP**,也不改写来源案例/故障的人工确认结果。

行为要点:

- 权限与来源:沿用生成权限(运维仅限自己提交的已通过案例;审核员不能调用;管理员不限创建者)。仅以指定已审核案例为依据,不额外检索其他知识;输入为可信来源快照(人工确认根因/处理/恢复验证 + 必要故障证据),不发送整份故障历史。调用前校验来源,模型返回后、创建草稿前重新读取来源状态与输入快照(列级读取),来源已变化不得落成有效草稿。
- 模型输出为独立固定 Schema(标题/适用环境及版本/现象/前置条件/排查步骤/处理方法/恢复验证/回滚方式/注意事项 + 待补信息 + 来源证据标注);`doc_id`/`version`/`status`/来源/创建者/维护人由后端决定,模型不能指定。材料缺失标注「待补/待人工确认」;推测性回滚标注「待验证」;来源 ID 由后端分配并校验。生成后仍需人工核对,Schema 通过不代表内容真实正确。
- 复用分析的模型适配、脱敏、限流与 JSON 有限修复(语法/结构错误共享一次修复预算,usage 逐次累计、缺失显示未知);失败只记安全元信息,不创建空白或半成品 SOP,不丢页面输入。`disabled` 明确不可用(模板/手工编辑不受影响);`demo` 显式模拟并标注;cloud/local 失败不回退演示或模板结果;`LOCAL_ONLY` 与 local 代理隔离覆盖本路径。
- 幂等:一次「点击生成」携带 `request_token`;同一次请求重复提交/重试沿用同一 token(不重复创建草稿、不重复计费);主动再次生成用新 token,可再建新草稿。生成记录存于独立表 `sop_ai_generation_runs`(不伪装成故障分析历史),含来源案例/输入快照标识、配置版本、真/演示、状态、调用次数/延迟/真实 usage、待补信息、失败类型与安全原因。
- 前端:生成中防重复点击;失败持久显示原因与人工重试;usage 缺失显示「用量未知」;云模式说明会向配置服务发送脱敏后的来源材料(无需逐次确认弹窗);过期响应不应用到其他案例。

### 显式演示内容(第四阶段第一批)

启动 seed 仍只负责内置分类与账号;演示业务材料必须显式执行,不在启动、登录或打开页面时自动写入:

```powershell
# 演示环境必须使用独立 Compose 项目名、独立 env 文件与独立端口
$demoProject = "opssop-demo-p4b1"
# .env.demo 已被 .gitignore 忽略;参考 .env.example 设置
# BACKEND_PORT=18000、FRONTEND_PORT=18080、POSTGRES_PORT=15432、
# FRONTEND_HOST=127.0.0.1、OPSSOP_ENV=development、OPSSOP_SEED_DEMO=1、
# AI_MODE=demo、EMBEDDING_MODE=disabled、LOCAL_ONLY=1
docker compose -p $demoProject --env-file .env.demo up --build -d

# 在已启动的隔离环境中(库内已有 operator/reviewer 账号)
docker compose -p $demoProject --env-file .env.demo exec backend `
  python scripts/seed_demo_content.py --operator operator --reviewer reviewer

# 只列场景,不连接/写入数据库
docker compose -p $demoProject --env-file .env.demo exec backend `
  python scripts/seed_demo_content.py --list

# 执行前只读核对项目名/卷名/端口均为本轮专用资源
docker compose -p $demoProject --env-file .env.demo config
```

- `OPSSOP_ENV=production` 拒绝执行(退出码 2);未执行时普通业务不新增演示记录。
- 稳定标识表 `demo_seed_records(scenario_key, entity_role)`:重复执行跳过已有实体,改标题/改人工确认内容也不会重复创建或被覆盖;失败整体回滚。
- 正向场景:OOMKilled、探针配置错误、Service 端口不匹配、无就绪后端、DNS 失败、证书过期、Redis 连接问题;另含相同现象「接口超时」不同根因(DNS vs Redis)。
- 边界场景:信息不足、知识库无匹配、日志含提示词注入文本;注入文本只作为待分析数据保存,不执行命令、不访问外部、不改变权限。
- 负面夹具:待审/退回案例、草稿/待审/历史/废弃 SOP;正式检索只返回已通过案例与当前有效已发布 SOP。
- 预置案例/SOP 明确标注「演示预置」,不伪称由 AI 自动解决或审核;演示内容不是独立评测集。
- 命令只接受已存在账号的用户名,不创建/修改账号,不提供清库、全量覆盖或删卷行为。

### embedding 适配与索引管理(第三阶段第二批)

本批交付 embedding 适配层、pgvector 存储与管理员可控的索引构建/状态/重建;
正式检索与右侧助手的接入见下一节(第三批)。

**embedding 独立适配**(`backend/app/ai/embeddings.py`,与聊天模型独立配置):

| 模式 | 说明 |
|---|---|
| `disabled`(默认) | 不调用 embedding,不生成任何伪向量;基本业务与关键词检索不受影响 |
| `cloud` | 调用 `EMBEDDING_BASE_URL` 指定的 OpenAI 兼容 `/embeddings` 接口 |
| `local` | 调用本机/内网接口(受信任目标校验;不继承 HTTP(S)_PROXY 环境代理) |

- 严格校验返回:条数一致、索引 0..n-1 对应、维度一致、非空向量、数值有限(NaN/Inf 拒绝);
- 超时(`EMBEDDING_TIMEOUT_SECONDS`)、批量上限(`EMBEDDING_BATCH_SIZE`)、单条文本长度
  (`EMBEDDING_MAX_INPUT_CHARS`);超时/连接/5xx 有一次带短退避的有限重试,
  **429 限流不重试、不退避**,由调用方直接降级(检索)或失败(索引),避免立即重发;
  失败不自动切换服务或切云;
- `LOCAL_ONLY=1` 同时约束聊天与 embedding(cloud 冲突在启动与调用双重拒绝);
- 所有发送文本在最终请求边界统一脱敏;密钥不进日志/错误/前端;usage 缺失保持未知,不虚构。
- **真实服务已联调(2026-10-27)**:OpenAI 兼容端点 + `qwen3.7-text-embedding-flash`(1024 维)
  完成真实索引构建(19 单元、2,197 tokens)与 6/24 条两组检索评测;混合检索在 24 条合成样本上
  **未显示优于关键词**(Recall@3 0.9333 vs 0.9667、无答案 5/9 vs 7/9),且额外命中仍全部是
  已发布知识、无草稿/历史版本泄漏。详情与限制见文末验证记录。

**索引结构**(PostgreSQL + pgvector,镜像 `pgvector/pgvector:pg16`):

- `embedding_index_units`:索引单元 = 一个来源文档(`unit_key=doc`),记录来源类型/ID、
  SOP 版本、内容指纹(sha256)、代次 generation、配置指纹、实际维度与向量列;
- `embedding_index_state`(单行):状态、活动代次及其指纹/模型/维度/文本版本、构建代次/领取计数、
  实际数量、安全失败原因;
- 纳入范围仅**已通过案例 + 当前有效已发布 SOP**(不索引待审/退回案例、草稿/历史/废弃 SOP、
  未解决故障,不复制故障历史);文本为知识字段结构化拼接,先脱敏后截断;
- 配置指纹 = `sha256(模式|端点|模型|文本版本 t1)[:16]`(**不含密钥**):相同维度不同模型不混用;
  模型/端点/文本版本变化后状态显示「需要重建」,旧向量不静默继续使用。

**构建/状态/重建命令**(管理员;状态查询为纯读取,登录/启动/页面加载不会自动构建):

```bash
# 查看索引状态(不触发模型调用):未配置/待构建/构建中/可用/需要重建/失败
docker compose run --rm backend python scripts/build_embedding_index.py --status

# 构建/刷新(同指纹同内容直接复用向量,不重复计费、不重复记录)
docker compose run --rm backend python scripts/build_embedding_index.py

# 强制重建(新代次;失败不激活半成品,与当前配置兼容的旧代次保留可用)
docker compose run --rm backend python scripts/build_embedding_index.py --rebuild

# 管理 API 等价(仅管理员;普通运维/审核员 403)
#   GET  /api/admin/embedding-index      状态
#   POST /api/admin/embedding-index/build    构建/刷新
#   POST /api/admin/embedding-index/rebuild  强制重建
```

构建/重建会把**脱敏后**的知识内容发送至配置的 embedding 服务(cloud 模式为出网操作,
范围仅限已通过案例与当前有效已发布 SOP 的文本;API 响应含 `egress_notice` 说明)。

**失败与恢复**:构建失败丢弃半成品代次(不标记为可用),原因脱敏后落状态行;
与当前配置兼容的完整旧代次保留可用,不兼容标「需要重建」;构建期间来源变化/移除的条目
不落为有效索引并在结果 `skipped` 中明示(部分成功不宣称完整构建);
中断的执行者超时后按失败回收,重新执行构建命令即恢复;
并发构建由数据库原子领取限制为单一执行者,被接管/配置切换的旧任务不能提交为当前有效结果。

**迁移与本地库处理**:数据库镜像切换为 `pgvector/pgvector:pg16`(基于 postgres:16,
沿用 `pgdata` 卷,不删库);迁移自动 `CREATE EXTENSION IF NOT EXISTS vector`,
镜像缺失或权限不足时给出明确错误,不用普通 JSON 数组冒充。
若某本地库已执行**旧版** `f7e8d9c0b1a2`(第一批未发布迁移曾原地加列),单纯 `upgrade head`
不会补 `attempt`/`field_sources` 列,执行
`docker compose run --rm backend python scripts/repair_generation_runs_columns.py`
幂等补列(不改写任何既有业务数据)。

### 向量检索、混合检索与可靠性(第三阶段第三批)

正式知识检索 API(`/api/search`)与右侧助手的共同后端路径(`app/search/hybrid.py`)
在关键词检索之上融合向量候选;**embedding 不可用时自动降级关键词并标注安全原因**,
基本业务不受影响。检索只作知识推荐:不自动确认根因、不合并案例、不发布 SOP、不执行命令。

**向量查询与兼容性**:
- 查询文本经第二批适配层嵌入(最终请求边界脱敏、长度/超时;超时/连接/5xx 一次有限重试,
  **429 只请求一次即降级**,不立即重发;disabled 零调用;失败不循环、不切服务);
  查询向量使用**独立限流/并发配额**
  (`EMBEDDING_RATE_LIMIT_PER_MINUTE`/`EMBEDDING_MAX_CONCURRENT_PER_USER`),
  与聊天分析配额(`AI_RATE_LIMIT_PER_MINUTE`)分离:检索不挤占分析额度,缓存命中
  不占聊天额度;向量限流时降级关键词并标注原因;
- 只使用与当前配置兼容的活动索引(配置指纹/模型/维度/文本版本);查询向量维度必须与
  活动索引一致;同维度不同模型不混用;
- pgvector 距离计算在数据库内完成(`ORDER BY embedding <=> :qvec`),不在 Python 端排序;
  **当前状态/权限/有效版本约束在距离排名与候选 LIMIT 之前过滤**(废弃/换版/无权近邻
  不占候选池);内容指纹陈旧等无法 SQL 过滤的候选剔除后**有界补取**(窗口按需扩大,
  单请求最多考察 60 行),失效近邻不耗尽候选池;
- 一次请求绑定一个索引代次;查询期间重建切换代次最多重读一次(不重复 embedding 调用),
  仍不一致则降级;明显不相关的向量候选(余弦距离 > 0.6)剔除,不强行凑结果。

**混合排序**:RRF 融合(`Σ 1/(60+rank)`,不相加不同量纲分数),来源
(source_type/source_id/version)去重、稳定排序、top_k≤8、候选池有上限;
不展示未经校准的置信百分比;不引入付费 reranker。

**来源过滤与最终复核**(最高优先级):排序/截断前即满足权限与正式知识状态(仅已通过
案例 + 当前有效已发布 SOP;草稿/待审/退回/废弃/历史版本双路排除);索引行做内容指纹
陈旧识别(来源内容更新后旧向量不是最新依据,直接剔除);**全部返回出口(成功、限流、
配置异常、维度异常、embedding 失败降级)统一执行返回前来源复核**;片段从当前来源字段
生成,索引 `text_excerpt` 不作为可信正文;助手只接收最终校验通过的片段,且**模型请求
发出前再次按当前状态/版本复核**:失效内容剔除(失效知识不发给模型;返回后的引用清洗
无法撤回已发出内容);仍有效来源**保留查询相关片段**——旧命中片段仍可在当前来源验证
(列级直读,绕过会话陈旧实例)则保留其命中位置,来源变化后按原查询在当前字段重新定位
(与正式检索共用提取逻辑:按加权得分选最佳命中字段,弱词命中标题不掩盖错误码命中
排查/处理字段),不把排查依据统一替换成通用现象前缀;片段长度与最终脱敏约束保持。
模型返回后仍按当前来源复核引用;新草稿不替换旧发布版本,换版后旧版本不再是依据。

**缓存**:分析缓存键并入检索策略 + 活动代次 + 配置指纹(策略/代次/配置变化自动失效);
缓存命中仍执行来源复核,不跳过权限检查;请求倒序、切换故障、组件卸载保护保持。

**响应与状态未知保护**:`retrieval_mode`(keyword/hybrid)、`degraded`、`degrade_reason`
如实标注;关键词无匹配、向量无候选与 embedding 失败可区分;全部检索失败不伪装
「知识库无答案」。`GET /api/search?keyword_only=true` 为**明确的服务端纯关键词路径**
(零 embedding 调用/零外发):AI 状态未知(状态接口失败/迟到)时前端只走该路径并提示
「本次仅关键词检索」,关键词基本能力不依赖模型可达;状态确认为 cloud 后才允许混合检索
并展示出网说明(不逐次弹窗);新检索失败后旧方式标签/旧降级横幅不残留。

**评测**(独立库 `opssop_eval`,与演示/业务数据分开):

```bash
# 合成向量评测(验证脚本/排序机制/统计口径;非真实语义效果;--setup 写入夹具)
docker compose run --rm -e EMBEDDING_MODE=cloud -e EMBEDDING_BASE_URL=https://synthetic.invalid/v1 \
  -e EMBEDDING_MODEL=synthetic-eval backend python scripts/eval_retrieval.py --setup --synthetic
# 复跑(夹具已在;每次评测强制重建索引,合成/真实向量来源不混用)
docker compose run --rm -e EMBEDDING_MODE=cloud -e EMBEDDING_BASE_URL=https://synthetic.invalid/v1 \
  -e EMBEDDING_MODEL=synthetic-eval backend python scripts/eval_retrieval.py --synthetic
# 真实效果评测(配置真实 embedding 服务并授权后):强制构建真实来源索引再评测
docker compose run --rm backend python scripts/eval_retrieval.py --setup
# 删除评测夹具
docker compose run --rm backend python scripts/eval_retrieval.py --cleanup
```

指标口径:Recall@3/Recall@5(gold 入前 3/5 比例)、MRR(首个 gold 排名倒数)、
空结果正确率(gold 为空的查询完全未返回结果的比例,**任何命中均计误报**);查询集含
问法改写/错误码精确/相同表象不同根因/无答案/来源失效五类,人工标注 gold。
退出状态:0=混合指标有效(全部查询为 hybrid);2=配置/夹具/索引构建错误;
3=存在降级查询,混合指标标为**未验证/不可用**并给出降级率(不把关键词降级汇总成
混合效果)。真实评测入口必须构建真实来源索引(严格走配置适配器,不接受注入合成向量)。
合成 embedding 下的对比只验证机制与口径;
**没有真实 embedding 时不发布混合检索质量提升或真实 Recall 结论**(真实效果待运行)。

### 阶段 3 第二批:embedding 适配与索引管理(2026-10-15,交回复审)

已实现:见上一节「embedding 适配与索引管理(第三阶段第二批)」。变更文件:

- 新增 `backend/app/ai/embeddings.py`(适配层)、`backend/app/search/index_text.py`(文本组织/来源复核)、
  `backend/app/search/embedding_index.py`(状态机/领取/构建)、`backend/app/routers/admin_embedding.py`(管理 API);
- `backend/app/models.py` + 迁移 `b8c7d6e5f4a3_add_embedding_index.py`
  (`embedding_index_state`/`embedding_index_units`,pgvector `vector` 列,`CREATE EXTENSION`);
- `backend/app/config.py`/`.env.example`/`docker-compose.yml`:新增
  `EMBEDDING_TIMEOUT_SECONDS`/`EMBEDDING_BATCH_SIZE`/`EMBEDDING_MAX_INPUT_CHARS`,
  数据库镜像 `pgvector/pgvector:pg16`;`requirements.txt` 增加 `pgvector`;
- 脚本:`backend/scripts/build_embedding_index.py`(状态/构建/重建 CLI)、
  `backend/scripts/repair_generation_runs_columns.py`(旧版迁移补列,幂等);
- 测试:`tests/test_embeddings.py`(18 项)、`tests/test_embedding_index.py`(22 项)。
  **不改关键词检索与右侧助手策略**(向量检索未接入正式诊断,属第三批)。

已实际运行(合成向量,真实模型调用 **0** 次;本地 Windows + Docker Desktop):

- **后端 pytest 189 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,186s;含既有 149 项回归与迁移空库升级/回滚再升级)。新增 40 项覆盖:
  - disabled 零调用且业务/关键词检索正常;管理接口运维/审核员 403、管理员可用;
  - LOCAL_ONLY 拒 cloud(构造即拒)、local 不继承环境代理、失败只有限重试不切服务;
  - 索引文本与最终请求边界脱敏(整份请求断言);错误/描述不含密钥;
  - 条数/索引/维度/空向量/NaN/Inf/非数值坏响应与批间维度不一致均失败,半成品清理、不激活;
  - 仅已通过案例与当前有效已发布 SOP 纳入(待审/退回/草稿不纳入);
  - 构建期间来源改写 → `skipped` 明示且不落为有效索引(result=partial,不宣称完整);
  - 同模型同内容重复构建 0 次调用/不重复记录;内容变化重新嵌入;
  - 同维度换模型 → needs_rebuild,重建后全量绑定新指纹;维度变化同理;
  - 构建期间配置切换 → 409,结果不提交、半成品清理;
  - **双线程屏障并发领取恰一个执行者**;过期接管后旧执行者 409、半成品清理、attempt=2、新代次未被污染,接管后可恢复构建;
  - 重建失败保留兼容旧代次(ready+原因可见)/不兼容标 needs_rebuild;中断执行者超时回收可恢复;状态查询零模型调用。
- **迁移路径**:空库升级与升级-回滚-再升级(一次性库)通过;开发库从 `a1b2c3d4e5f6` 实际
  `upgrade head` 至 `b8c7d6e5f4a3`(不删库,`pgdata` 保留),`embedding` 列为真实 pgvector `vector`;
  旧版迁移补列脚本对已含新列库为幂等无损(输出列清单)。
- **前端 vitest 29 项全部通过**、`vue-tsc -b && vite build` 通过(本批无前端变更,回归确认)。
- **CLI 实测**:`--status` 读取状态不触发模型;构建/重建命令可重复执行。

尚未验证(如实标注):

- **真实 embedding 联调**:未配置真实服务,未调用;合成向量只证明协议/存储/状态机制,
  **不能证明语义检索准确率**;local 真实模型、其他云服务未联调。
- 向量检索/混合检索(第三批)未开始,索引尚未被任何检索路径消费;Linux 云部署、备份恢复未验证。
- 真实并发 HTTP 层屏障探针待审查方执行(本批并发证据为双独立会话 + `threading.Barrier`
  同时领取的数据库竞争,以及嵌入钩子窗口内的接管注入)。

### 阶段 3 第二批复审修复(2026-10-15,按《mimo-第三阶段第二批独立复审-2026-10-15.md》修复 P2-1~P2-3)

已实现(修复内容):

- **P2-1 状态查询不再覆盖新构建状态**(`embedding_index.py`):`get_status` 改为列级只读快照
  (绕过 identity map),移除无条件状态同步写入;过期中断执行者的回收改为**条件更新**
  (按读取时 attempt+代次+状态+过期时间,命中才写入),未命中(已被新执行者领取/已完成)
  则重新读取——迟到的 GET 不能取消新领取者,也不能把新 building 覆盖成失败。
- **P2-2 强制重建贯通**:新增 `run_build(..., force=)`;`POST /rebuild` 与 CLI `--rebuild`
  强制绕过同指纹同内容的向量复用,整代重新嵌入并校验维度(可刷新同端点同模型的旧向量/
  损坏向量/服务维度变化);`POST /build` 与普通构建保留零调用复用;强制重建失败仍不破坏
  兼容旧代次。
- **P2-3 配置冲突退出结束自身租约**:配置切换导致结果不提交时,按 attempt+代次条件更新
  结束自己的 building 租约(已被接管则不改新执行者状态),失败原因(`config_switch`)落
  状态行,状态按**当前配置**与活动代次判定(pending/failed/needs_rebuild/ready);
  新配置可立即重新构建,不遗留约 30 分钟的租约阻塞。

已实际运行(合成向量,真实模型调用 **0** 次):

- **后端 pytest 194 项全部通过**(含既有 189 项回归)。新增 5 项复审回归:
  - P2-1 屏障时序复现:旧 GET 读取后新领取者接管,回收写入落空并重读——attempt=2 保持
    building、未被标 interrupted,后续 POST 409,新领取者可正常提交(active_generation=2);
  - P2-2 同端点/同模型/同内容强制重建:实际调用 1 次、reused=0、维度 8→16 刷新;普通重复
    build 仍零调用复用;CLI `--rebuild` 与 API 一致(force 参数贯通,经行为断言);
  - P2-3 配置切换退出后 GET 非 building 且 `config_switch` 原因可见、立即重建成功;
    被接管后旧执行者的失败收尾返回 False,不释放新执行者租约。
- 前端本批无变更(上一轮 29 项/构建通过且源码未变,未重跑)。

尚未验证:与上一轮相同——真实 embedding/local 模型、Linux 云部署、备份恢复、第三批混合检索未验证。

### Windows 本地开发注意事项

容器内的 `localhost` 指向容器自身。后端容器访问宿主机上的本地模型服务(Ollama 等)需使用 `host.docker.internal`;Linux 云服务器则使用实际内网地址。Linux 云服务器不能访问你 Windows 电脑上的本地模型。仅云端服务实际可达的地址才有效,本项目不自动配置公网暴露或隧道。

## 独立评测(第四阶段第二批)

### 版本化合成评测集

`backend/eval_data/v1/` 将知识夹具、样本输入、参考标签和合成模型行为分开存储:

| 文件 | 内容 |
|---|---|
| `manifest.json` | 数据集版本 `2026-10-22.1`、标签来源/审核状态、样本/知识计数 |
| `knowledge.json` | 25 条稳定键知识夹具(案例/SOP,含待审、退回、草稿、待审、历史、废弃和当前 v2) |
| `samples.json` | 39 条样本;检索 24 条、分析 21 条,另有失败注入/缓存/权限样本 |
| `labels.json` | 合成参考标签(provisional,人工复核标记为 false;不是真人 gold) |
| `synthetic_behaviours.json` | timeout、429、非法 JSON、Schema 错误/修复、无效引用、部分 usage、调用期间来源失效 |

- 稳定来源键形如 `kb:case-oom`,夹具准备后才映射到实际案例/SOP ID 与 SOP 版本;不依赖数据库自增 ID 或标题。
- 分组覆盖:明确分类/精确错误、问法改写、相同表象不同根因、信息不足、知识库无答案、注入文本、权限/状态边界、版本链。
- 负面夹具预先写明审核/发布状态;正式检索排除它们,控制查询仍可返回公开已通过/已发布来源。
- 数据集不使用真实业务日志、个人资料或第一批 demo 输入;标签是开发 AI 编写的合成参考,不能称为已人工审核。

### 运行入口

主入口为 `backend/scripts/eval_suite.py`(默认 synthetic,零真实模型/embedding 调用、无 API Key):

```powershell
$demoProject = "opssop-demo-p4b1"

# 在隔离演示环境执行完整合成评测(检索 + 分析)
docker compose -p $demoProject --env-file .env.demo exec backend `
  python scripts/eval_suite.py --suite all --mode synthetic

# 只跑检索/分析
docker compose -p $demoProject --env-file .env.demo exec backend `
  python scripts/eval_suite.py --suite retrieval --mode synthetic

# 精确清理本轮评测库(需要上次输出的 target_db 与 run_id)
docker compose -p $demoProject --env-file .env.demo exec backend `
  python scripts/eval_suite.py --cleanup --target-db opssop_eval_<run_id> --run-id <run_id>
```

- 每轮创建 `opssop_eval_<run_id>` 专用库,使用正式 Alembic 迁移;`eval_run_manifest` 记录 run_id、数据集版本/哈希和夹具映射。
- 目标库名在连接前校验:必须匹配 `opssop_eval_*`、包含本轮 run_id,且不得是业务库、测试库、demo 库或系统库;production 直接拒绝。
- `--reuse` 仅在 manifest 的 run_id/数据集哈希匹配时允许;`--keep-db` 保留库供检查;默认成功后自动按 manifest 精确删除本轮库,不使用通用 `down -v`。
- 退出码:0 完成且机制断言通过;2 配置/数据集/数据库准备错误;3 混合降级导致混合指标不可用;4 机制断言失败/运行中断/清理失败。退出 0 **不隐含真实模型效果好**。
- 结果输出 `scripts/eval-artifacts/<run_id>/report.json` 和可读 `summary.md`(Git 忽略)。报告含每样本结果、计数/分母/状态、原始/最终引用观察、耗时、失败/缓存/权限分 suite 汇总。
- 在容器内运行时,输出默认位于容器文件系统;请用 `--out` 指向宿主挂载目录,或运行后 `docker cp` 到宿主机。仓库主机直接运行时默认写入仓库 `scripts/eval-artifacts/`。
- `--mode real` 需显式 `--allow-real`;本批不执行真实模型/embedding 效果评测。
- 旧入口 `backend/scripts/eval_retrieval.py` 保留:现在也使用每轮安全评测库、标签清理和 `--cleanup --target-db/--run-id`;真实 embedding 旧入口需显式 `--allow-real-embedding`。旧 6 查询契约与退出码 0/2/3 保留,新入口是版本化主入口。

### 指标口径

- 所有比例输出 `numerator / denominator / value / status / definition`;分母为 0 时为 `null + not_applicable`,不填 0% 或 100%。
- 检索:Recall@3/5、MRR 按 gold 非空查询宏平均;失败查询计 0;同一来源按 `(source_type, source_id, version)` 去重;SOP 版本必须正确;无答案查询只有 HTTP 成功且 0 命中才算正确,接口失败不算正确空结果。
- 混合:任一正常查询降级/失败,整批混合指标标 `unverified`,不发布未降级子集;关键词指标仍可读;合成向量只验证机制,不证明真实语义提升。
- 分析:主分类正确率的分母包含成功、失败、无建议、非法分类样本;応弃答、incomplete 单独统计;失败率含 HTTP 错误和业务 `failed`,同一请求不重复计。
- 引用:分别统计模型清洗前初次/采用输出、业务清洗后最终引用;无效/未下发/版本不符/来源失效有原因计数;不可解析样本记为不可观察,不当作 100% 有效;格式修复初次/最终分开。
- 耗时:进程内 TestClient 单调时钟,均值/P50/P95(最近秩),不含迁移/夹具/索引构建;缓存、失败注入、权限 suite 分开;不能冒充生产网络端到端性能。
- 真实人工修改比例没有真人复核数据时为 `not_available`;另列的参考标签不一致率明确标注为推算,不是人工修改比例。
- usage 缺失保持未知,部分字段不补造总量;真实聊天/embedding 调用为 0,合成调用与真实调用分开计数;不计算费用。

## 数据、备份、恢复与迁移

业务数据保存在 Compose 命名卷(默认 `<project>_pgdata`),应用部署在哪数据就保存在哪。
第四阶段第三批提供**二进制安全**的备份恢复工具与隔离演练,不再使用文本管道备份:

```powershell
# PowerShell 7:备份(pg_dump -Fc + 元信息 + SHA-256;默认拒绝覆盖)
python backend/scripts/ops_cli.py backup `
  --project opssop-demo-p4b1 --env-file .env.demo `
  --compose-file docker-compose.yml --run-id backup20261022a `
  --out-dir "D:\opssop-backups"
```

```bash
# Linux:恢复到全新目标库(已存在/受保护/来源库一律拒绝)
python3 backend/scripts/ops_cli.py restore \
  --project opssop-demo-p4b1 --env-file .env.demo \
  --compose-file docker-compose.yml --run-id restore20261022a \
  --archive /var/backups/opssop/opssop-backup20261022a.dump \
  --metadata /var/backups/opssop/opssop-backup20261022a.json \
  --target-db opssop_restore_restore20261022a \
  --out-dir /var/backups/opssop/restore
```

- 完整命令、目标保护、角色/权限、失败/取消策略、升级回退:
  [docs/backup-restore.md](docs/backup-restore.md)。
- 生产 Compose、预检、HTTPS/网络边界、本地 Linux 容器验证范围:
  [docs/deployment.md](docs/deployment.md)。
- 隔离演练、资源归属与清理:
  [docs/operations.md](docs/operations.md)。
- 迁移:后端启动时先做配置预检,再自动 `alembic upgrade head`,再 seed/账号检查,最后启动 API;
  手动执行 `docker compose run --rm backend alembic upgrade head`。
- 备份归档、元信息、演练环境文件与日志留在仓库外或 Git 忽略目录;归档含账号哈希和业务内容,
  按私有数据保存,不提交公开仓库。项目不自动向其他存储同步。

## 测试

```bash
# 在后端容器中运行(使用独立测试库 opssop_test,与业务库 POSTGRES_DB 强制隔离)
docker compose run --rm --no-deps backend python -m pytest tests/ -v

# 前端组件行为测试(vitest + happy-dom,不依赖后端;位于 frontend/tests/)
cd frontend && npm test

# API 级业务闭环(需显式提供凭据,指向独立测试环境)
export E2E_BASE_URL=http://localhost:8000
export E2E_OPERATOR_PASSWORD=... E2E_REVIEWER_PASSWORD=... E2E_ADMIN_PASSWORD=...
python scripts/e2e_loop.py

# 浏览器完整闭环(需 Playwright 与系统 Chrome)
export E2E_BASE_URL=http://localhost:8080
export E2E_OPERATOR_PASSWORD=... E2E_REVIEWER_PASSWORD=...
python scripts/browser_e2e.py

# 第四阶段 demo 浏览器闭环(前置:AI_MODE=demo、EMBEDDING_MODE=disabled、已执行演示 seed)
# 演示前端使用独立端口(如 18080);生产预览示例:
#   cd frontend; $env:BACKEND_PORT="18000"; npm.cmd run build;
#   npm.cmd run preview -- --host 127.0.0.1 --port 18080
$env:E2E_BASE_URL = "http://127.0.0.1:18080"
$env:E2E_OPERATOR_PASSWORD = "<合成口令>"
$env:E2E_REVIEWER_PASSWORD = "<合成口令>"
$env:E2E_ADMIN_PASSWORD = "<合成口令>"   # 可选:跨账号权限断言;省略则该项标记 skipped
python scripts/demo_browser_e2e.py

# 第四阶段第二批 合成评测(隔离演示环境;默认零真实模型/embedding 调用)
docker compose -p opssop-demo-p4b1 --env-file .env.demo exec backend \
  python scripts/eval_suite.py --suite all --mode synthetic
# 精确清理本轮评测库(使用输出中的 target_db/run_id)
docker compose -p opssop-demo-p4b1 --env-file .env.demo exec backend \
  python scripts/eval_suite.py --cleanup --target-db opssop_eval_<run_id> --run-id <run_id>

# 模型适配层真实接口冒烟(需 .env 已配置 AI_MODE=cloud|local;不写库、不打印密钥)
docker exec -i mimo-backend-1 python - < backend/scripts/smoke_llm.py
```

重点覆盖:权限越权、状态流转、版本链、SOP history 跨用户可见性、审核完整性(提交/审批双侧)、人工确认不可绕过、测试库隔离硬校验、迁移升级-回滚-再升级往返、生产默认账号检查、bcrypt 72 字节上限、连接串特殊字符;第二阶段新增:分析越权 403、空输入 400、demo 标注与 usage 未知、缓存复用不重复计数、历史倒序、模型超时/非法 JSON 不丢数据不编造、引用与分类校验(无效引用移除+不完整标注)、限流 429、检索过滤(草稿/未发布不进入)、配置版本下发;第二阶段审查修复:缓存隔离(跨故障/跨用户/TTL)、模型请求全量脱敏、local 模式代理隔离、组件初始化 TDZ、防抖/关开关/在途补证排队、SOP 提交锁定编辑、引用来源复核、无证据事实剔除、多调用用量聚合;第四阶段第一批新增:案例来源直达/刷新/登录回跳、来源链接只由受控 ID 构造、403/404/非法 ID 状态、切换来源不串数据、演示 seed 幂等与 production 拒绝、负面夹具检索排除、demo 零调用/根因不被 AI 确认/草稿不自动发布、分析/补证/生成失败不丢输入;第四阶段第二批新增:版本化数据集与稳定键、检索 Recall@3/5/MRR/无答案宏平均与 SOP 版本匹配、分析分类/弃答/失败分母、原始/最终引用观察与格式修复、调用期间来源失效、缓存复用零新增调用、进程内 API 耗时 P50/P95、混合降级整批不可用、评测库连接前隔离与按 manifest 精确清理。

## 已验证 / 未验证

见「当前状态」;具体验证输出随每次实施记录在下方「验证记录」。没有真实模型时,评测脚本交付待运行版本,模拟测试与真实效果严格区分。

## 验证记录

### 阶段 1 首轮(2026-10-13,本地 Windows + Docker Desktop)

已验证(实际运行):

- 全新数据库迁移:空库 `alembic upgrade head` 建表成功(初始迁移 `c53e6658b32f`)。
- Docker Compose 启动:db(healthy)、backend(healthy)、frontend 全部启动,nginx `/api` 代理正常,前端首页 200。
- 业务闭环端到端(`python scripts/e2e_loop.py`,27 项全通过):创建故障 → 追加检查记录 → 人工确认解决(必填根因/处理/恢复)→ 生成待审核案例 → 审核通过 → 从案例生成 SOP 草稿 → 提交 → 发布 → 修改已发布 SOP 生成新版本 → 新版本发布后旧版自动转历史 → 已有未发布草稿时再开新版被拒(409)→ 工作台统计来自数据库 → 审计事件(fault.resolve / case.approve / sop.publish / sop.new_version)→ 管理员分类增改删 → AI 状态为 disabled。
- 越权被拒:运维审核案例 403、审核员创建故障 403、运维查看账号列表 403、运维查看他人故障 403(脚本 + pytest 覆盖)。
- pytest 22 项全通过:认证 5、权限 7、工作流 10(含完整闭环、退回重提、SOP 版本链、废弃、反馈、提交校验)。
- 服务重启后数据保留:`docker compose restart` 后故障记录完整。

### 阶段 1 质量修复(2026-10-13,按《mimo-第一阶段质量检查-2026-10-13.md》增量修复)

已验证(实际运行,均为本地 Windows + Docker Desktop):

- **pytest 52 项全部通过**(`docker compose run --rm --no-deps backend python -m pytest tests/ -q`,84.8s):
  - 回归新增 30 项,覆盖质量检查缺陷:SOP history 跨用户私有草稿与公开旧版泄漏(P1 #1)、创建/更新 resolved 绕过人工确认(P1 #2)、生产默认口令/演示账号检测与首次建号不覆盖口令(P1 #3)、测试库隔离硬校验与 seed 只写测试库(P1 #4)、退回 SOP 绕过提交直接发布(P2 #7)、待审核案例快照锁与提交/审批完整性(P2 #8)、迁移升级-回滚-再升级往返(P2 #9)、连接串特殊字符转义(P2 #10)、bcrypt 72 字节上限与中文密码(P2 #11)、工作台有效 SOP 总数、维护人下拉接口权限。
- **迁移往返**:一次性库上空库升级 + `upgrade → downgrade → re-upgrade` 成功,回滚后无残留枚举类型(`tests/test_migrations.py`,每次运行随机建库并删除)。
- **测试库隔离**:全量测试后检查业务库,用户仍为 admin/operator/reviewer 3 个、既有故障 3 条未变;测试写入仅落在 `opssop_test`。
- **API 级业务闭环**(`scripts/e2e_loop.py`,凭据经环境变量注入、带唯一运行标记、可重复运行):27 项全部通过,含发生时间/标签录入、越权 403、案例审核、SOP 发布、新版本与旧版转历史、同文档未发布草稿时再开新版 409、工作台计数、审计事件。
- **浏览器完整闭环**(`scripts/browser_e2e.py`,Playwright 驱动系统 Chrome,21 项全部通过,运行标记 `[br:459e571c]`):
  登录 → 新建故障 → 补证 → 人工确认解决 → 案例审核 → 生成 SOP 草稿 → 提交 → 发布 → 生成新版本 → 发布 v2 → 旧版转历史;专项断言:
  - **未保存编辑保留(P1 #5)**:追加记录后描述编辑仍在;确认解决/提交审核前自动保存,成功后内容不丢失;
  - **历史版本切换(P1 #6)**:点击版本历史切换 v1↔v2,URL 与页面内容一致,操作按钮作用于当前显示记录;
  - reviewer 登录后不显示保存/追加/确认解决按钮(与后端 403 对齐)。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11);`package-lock.json` 已提交,Docker 构建改用 `npm ci` 锁定依赖。
- **服务重建与数据保留**:`docker compose build && docker compose up -d` 用新镜像重启后,业务库既有数据(3 用户 / 3 故障 / 2 案例 / 5 SOP)完好,未重置数据库、未覆盖 `.env`。

未验证(如实标注):

- 人工在浏览器中的自由探索式操作(本次浏览器闭环为 Playwright 驱动真实 Chrome 走查固定路径,非人工漫游测试)。
- 云部署(同一套 Compose 在 Linux 云服务器)。
- 真实模型接口(cloud/local)、demo 模拟分析、关键词/混合检索(第二、三阶段)。
- 备份恢复脚本实操(文档已给命令,未演练)。
- 百分号开头密码的 Alembic 路径仅函数级测试覆盖(P2 #10 的 `%` 插值已转义,未单独起库演练)。

### 阶段 2 AI 助手(2026-10-14,本地 Windows + Docker Desktop)

已验证(实际运行):

- **pytest 71 项全部通过**(`docker compose exec backend python -m pytest tests/ -q`,81s;含阶段 1 全部回归):
  第二阶段新增 19 项(`tests/test_ai_analysis.py`):
  - 分析触发:demo 标注(`is_demo`、summary 含「演示数据」、usage 为 null 不虚构)、未保存表单覆盖参与分析但不回写故障、空输入 400 且不落库;
  - 版本与复用:同输入/知识/配置版本复用原记录(reused=true,历史仍为 1 条)、历史倒序返回(最新在前)、ai-status 下发 `model_config_version`(响应无密钥字段);
  - 越权:运维分析/查看他人故障分析历史 403;审核员可读分析历史;
  - 模型失败:超时(LLMError)与非法 JSON(有限修复重试后)落 failed 记录,`report=null` 不回退预设答案,故障字段与输入完好;
  - 引用校验:无效证据 ID/引用 ID 移除、未知分类置空、标签去重限量、优先级非法回落、无有效片段的引用移除,并标注 `incomplete_reasons`;
  - 限流:超单用户频率返回 429;
  - 检索:仅已通过案例与当前有效已发布 SOP 进入结果,无匹配返回空不编造,未认证被拒。
- **迁移**:表清单断言补入 `ai_analyses`(空库升级 / 升级-回滚-再升级往返继续通过)。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11)。
- **服务重建**:`docker compose build backend && docker compose up -d backend` 后容器启动 healthy,新路由(`/api/faults/{id}/analyze`、`/api/faults/{id}/analyses`、`/api/search`)随应用加载(测试经 TestClient 调用)。
- **demo 模式 HTTP 冒烟(容器内实测,凭据经环境变量注入不落日志)**:`AI_MODE=demo` 启动后走真实 HTTP:登录 → 创建故障 → `POST /analyze` 201(`is_demo=true`、`status=success`、`usage=null`、summary 含「演示数据 / 模拟分析」)→ 同输入再触发 `reused=true`(复用缓存不重复计数)→ 分析历史倒序 1 条 → `GET /api/search?q=OOMKilled` 返回命中并带知识版本。冒烟产生的临时故障与分析记录已从开发库删除,后端已恢复 `.env` 配置重启,`/api/system/health` ok、前端 200。

未验证(如实标注):

- **真实模型接口(cloud/local)联调**:未授权、未配置目标接口,协议行为仅经测试桩(DemoAdapter / mock 适配器)验证;真实端点的结构化输出、超时表现、usage 字段以实际接口为准。
- demo 模式在浏览器中的交互(防抖 2 秒、过期响应丢弃、复制命令)已实现但未做浏览器自动化走查;Playwright 闭环仍为阶段 1 路径。
- Linux 云服务器部署、备份恢复演练、embedding/混合检索(第三阶段)。
- 前端对过期响应/429/取消的行为目前由代码评审与类型检查保障,无自动化浏览器断言。

### 阶段 2 质量修复(2026-10-14,按《mimo-第二阶段审查-2026-10-14.md》增量修复)

已验证(实际运行,均为本地 Windows + Docker Desktop):

- **后端 pytest 89 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,93.2s;含阶段 1/2 全部回归)。回归新增 18 项(`tests/test_ai_analysis.py`),覆盖审查缺陷:
  - **P1 #1 分析缓存隔离**:缓存键绑定故障+用户(跨故障/跨用户不复用他人或他故障的记录)、TTL 过期后重新分析、复用前重新校验行归属;
  - **P1 #2 凭据脱敏边界**:JSON 带引号键(`{"password":"x"}`)、带空格引号值(`password="my secret value"`、`"Authorization":"Bearer x y"`)等 8 类形态,在**完整模型请求(全部 messages)**边界断言打码;良性文本(password_count 等)不误伤;
  - **P1 #3 全字段脱敏**:标题、分类名、片段标题等所有进入模型的字符串统一脱敏(请求级断言,含以敏感词构造的分类名);
  - **P1 #4 local 模式代理隔离**:5 种 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 组合下 local 适配器 `trust_env=False`(不继承环境代理直连配置端点);cloud 保持 `trust_env=True`;
  - **P2 #9 引用来源复核**:模型在途期间 SOP 被废弃后,响应与分析历史中的失效引用移除并标注 `incomplete_reasons`(有效案例引用保留);
  - **P2 #10 证据绑定契约**:未绑定有效证据 ID 的 observed_facts 移除并标注不完整;
  - **P2 #11 多调用用量**:JSON 修复重试按次记录(15+28=43 汇总)、任一调用缺失 usage 时显示未知,不把单次调用值当作总量。
- **前端组件行为测试 8 项全部通过**(`cd frontend && npm test`,vitest + @vue/test-utils + happy-dom,`frontend/tests/`,mock API 不依赖后端):
  - **P1 #5 面板初始化**:首次进入无引用错误(TDZ 回归)并恢复分析历史;查看历史报告标记为非实时结果;
  - **P2 #7 在途补证**:分析在途期间补充证据,旧请求结束后自动重新排队分析最新输入(断言第二次请求体为最新输入);
  - **P2 #8 开关/防抖**:关闭自动分析后输入停顿不发请求;防抖窗口内关开关清除已排定计时器;
  - **历史时序**:切换故障后旧历史响应不覆盖新故障历史;
  - **P2 #6 SOP 提交锁定**:提交审核在途时锁定表单与保存按钮、保存在途时禁用提交按钮,请求完成后恢复。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11);组件测试位于 `frontend/tests/`,不进入 `vue-tsc`/生产构建。
- **服务重建**:`docker compose build backend && docker compose up -d backend` 后容器 healthy,`/api/system/health` 返回 ok,前端 8080 返回 200。

已实现(本轮修复代码):

- P1 #1 缓存键含故障/用户并复核归属;P1 #2 结构化日志与带引号键值脱敏;P1 #3 提示词全字段统一脱敏;P1 #4 local 适配器不继承环境代理;P1 #5 搜索状态声明前移修复 TDZ;P2 #6 保存/提交期间锁定编辑;P2 #7 在途补证记入待排队并在旧请求结束后重发;P2 #8 关闭自动分析清除计时器、发送前复检开关/限流;P2 #9 模型返回后、缓存语义与历史读取时复核引用(权限/状态/版本);P2 #10 无有效证据绑定的事实剔除;P2 #11 usage 按次记录汇总、缺失显示未知。
- 次要项:分析历史请求序号/故障 ID 防护(倒序响应不覆盖);未保存分类覆盖参与分析时解析为分类名再入提示词(一致性)。

未验证(如实标注):

- **真实模型接口(cloud/local)联调**:仍未授权、未配置目标接口,保持未验证;local 模式代理隔离仅在适配器层以 `trust_env` 参数断言,未在真实代理环境抓包验证出网行为。
- 本轮未重跑浏览器闭环(Playwright 仍为阶段 1 路径);429/请求取消的浏览器自动化断言仍缺失,前端时序行为由 vitest 组件测试覆盖。
- Linux 云服务器部署、备份恢复演练、embedding/混合检索(第三阶段)未验证。

### 阶段 2 修复复审增量(2026-10-14,按《mimo-第二阶段修复复审-2026-10-14.md》继续修复)

已验证(实际运行,均为本地 Windows + Docker Desktop):

- **后端 pytest 97 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,98.1s;含既有 89 项回归无一破坏)。本轮新增 8 项(`tests/test_ai_analysis.py`「第二阶段修复复审回归」),覆盖复审缺陷:
  - **P1 #1 常见序列化层脱敏**:转义 JSON 键(`{\"password\":\"x\"}`)、JSON unicode 转义键名(`pass\\u0077ord`)、键内空白(`" password "`)、单引号值转义(`\'second secret\'`)、bash 引号拼接(`'a'\''b'`)、转义 token/Authorization;以**最终模型请求(全部 messages)**边界断言无残留;
  - **P1 #2 先脱敏后截断**:整字段脱敏后再截断(长 PEM 截断后 BEGIN/END 不成对也不泄漏材料)、BEGIN 缺 END 保守打码到结尾、短字段截断边界;最终提示词保留统一脱敏兜底;
  - **P2 #5 缓存复用实时复核**:检索完成与缓存返回之间的并发窗口内 SOP 被废弃时,缓存命中路径实时列级复核,返回清理后的当前视图(失效引用移除、success→incomplete、标注失效原因),不写回历史记录;历史读取同契约;
  - **P2 #6 部分 usage 字段**:按字段完整性聚合——仅每次调用都出现的字段求和,缺失字段不按 0 计(单元边界 + 集成回归);
  - **次要项(分类清空)**:显式 `category_id=null` 后提示词分类字段为「未分类」,不沿用旧分类名。
- **前端组件行为测试 12 项全部通过**(`cd frontend && npm test`;含既有 8 项回归)。本轮新增 4 项:
  - **P2 #3 卸载失效**:卸载后在途请求结束不再补发自动分析(待排队任务与计时器整体失效,真实卸载测试);
  - **次要项(faultId 切换)**:切换故障后 analyzing 复位,新故障可立即自动分析(旧请求不得占用在途状态);
  - **P2 #4 状态未知**:分类与 AI 状态独立加载互不牵连;状态加载失败时禁用自动模型调用、显示未知提示与重试入口(未确认模式与出网提示前不发送);
  - **P2 #6 用量展示**:部分字段缺失的 usage 逐字段显示「输入 15 / 输出 未知 / 合计 未知」,不虚构合计。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11)。
- **服务重建**:`docker compose build backend frontend && docker compose up -d backend frontend` 后容器 healthy,`/api/system/health` 返回 ok,前端 8080 返回 200。

已实现(本轮修复代码):

- P1 #1 脱敏规则覆盖转义 JSON 键、unicode 转义键名、键内空白、单引号值转义、bash 引号拼接等常见日志形态(`backend/app/ai/sanitize.py`);P1 #2 提示词各字段先整段脱敏后截断,最终统一脱敏兜底(`_clip` + `_build_user_prompt`);P2 #3 面板卸载使在途请求与待排队补发整体失效;P2 #4 AI 状态独立加载、未知时禁用模型调用并提供重试入口;P2 #5 缓存命中返回前实时复核来源并返回清理后的当前视图(不改写历史);P2 #6 usage 仅聚合各调用均出现的字段,缺失即未知;显式清空分类按未分类入提示词;faultId 切换复位 analyzing。

未验证(如实标注):

- **真实模型接口(cloud/local)联调**:仍未授权、未配置目标接口,保持未验证。
- 脱敏规则按已复现的常见日志形态补齐,不要求识别任意编码;业务内部域名、非标准自定义凭据格式仍可能漏过(脱敏仅为纵深防御一层,数据不出网边界仍是 local 模式与 LOCAL_ONLY)。
- 浏览器闭环、Linux 云服务器部署、备份恢复演练、embedding/混合检索(第三阶段)未验证。

### 阶段 2 真实模型联调冒烟(2026-10-14,本地应用 → DeepSeek 云端 API)

已验证(实际运行):

- **cloud 真实接口冒烟**(`backend/scripts/smoke_llm.py`,经应用适配层 `OpenAICompatAdapter` 发起真实请求,不写库、不打印密钥):
  - 连通/鉴权/模型名:DeepSeek(OpenAI 兼容 `/chat/completions`,`deepseek-flash`)普通调用成功;usage 为接口实测值(该模型返回 `reasoning_tokens`,推理消耗计入 completion)。
  - JSON 模式(`response_format={"type":"json_object"}`)返回合法 JSON 并解析成功。
  - 真实失败形态:DeepSeek 要求 json_object 模式下提示词含「json」字样,否则 400 `invalid_request_error`;据此将 json_mode 由「能力全局开关」改为「按请求声明」,普通请求不再携带 `response_format`。
- **后端 pytest 98 项全部通过**(容器内 `python -m pytest tests/ -q`,97.9s;含既有 97 项回归 + 新增 `test_json_mode_only_when_requested`):`response_format` 仅在请求开启且服务支持时携带。另修复测试与部署环境隔离:conftest 固定 AI 限流配置,防止真实 `.env` 的 `AI_RATE_LIMIT_PER_MINUTE=2` 泄漏进用例(修复前 `test_history_desc_order` 第 3 次请求被 429)。

已实现(本轮代码):

- `LLMRequest.json_mode` 按请求声明是否使用 json_object;适配层仅在「请求开启且 `AI_SUPPORTS_JSON_MODE=1`」时携带 `response_format`;结构化分析请求(`service.py`)开启,JSON 修复重试沿用(`backend/app/ai/llm.py`)。
- 可重复冒烟脚本 `backend/scripts/smoke_llm.py`;测试限流配置与部署环境隔离(`backend/tests/conftest.py`)。
- 本轮 `llm.py`/`service.py` 改动已同步进运行中的后端容器验证;下次 `docker compose build backend` 重建镜像时自动包含。

未验证(如实标注):

- **仅验证 DeepSeek 一家云端接口**;其他云服务、local 模式(Ollama 等)仍未联调。本机应用 → 云端 API 这一条路径通过,不代表其他环境/其他服务通过。
- 完整 AI 分析链路(HTTP 触发 `/analyze` 走真实模型生成报告)未做端到端实测;浏览器内真实分析走查未做。
- Linux 云服务器部署、备份恢复演练、embedding/混合检索(第三阶段)未验证。

### 阶段 2 云端冒烟增量复审修复(2026-10-14,按《mimo-云端冒烟增量复核-2026-10-14.md》修复)

已验证(实际运行,均为本地 Windows + Docker Desktop):

- **后端 pytest 113 项全部通过**(容器内 `python -m pytest tests/ -q`,102s;含既有 98 项回归)。本轮新增 15 项:
  - **复审 P1 嵌套 JSON 带分隔符密码(2 项)**:密码含逗号/分号/右花括号时整值打码、尾部不残留(单测),并以最终模型请求整份消息断言三种变体(含尾部)不外发;
  - **复审 P2 检索来源复核陈旧 ORM(5 项)**:独立事务废弃后 `_finalize_hit` 必须读到新状态(旧 `db.get()` 会返回 identity map 旧实例)、换版后旧版本命中剔除、`search_knowledge` 窗口内废弃不返回、`/api/search` 实际路由同断言、送入模型的知识片段不含窗口内废弃的旧正文(明示知识库无匹配);
  - **冒烟判据(8 项,`tests/test_smoke_llm.py`)**:demo/disabled 模式拒绝且 0 次调用、`AI_SUPPORTS_JSON_MODE=0` 记跳过/未验证不计通过、JSON 响应必须裸 JSON 对象(列表/标量/围栏/杂文判失败)、输出区分调用模式/已执行/未验证、报告不含密钥。
- **前端 vitest 16 项全部通过**(`cd frontend && npm test`;含既有 12 项回归)。新增 4 项(`frontend/tests/searchStaleness.spec.ts`):
  - Enter 并发搜索旧响应(redis 后到)不覆盖新查询(nginx)结果与输入框;
  - 旧请求失败不修改 error,且不改掉新请求的 loading;
  - 切换故障后旧检索响应不写入新故障面板(状态复位);
  - 卸载后旧检索响应整体失效。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11;主包大小提示仍存在)。
- **真实云端冒烟(收紧判据后)**:DeepSeek 普通调用与 JSON 模式均按新判据通过——JSON 项直接解析原始响应为裸 JSON 对象,usage 为接口实测值;输出区分「调用模式/已执行/未验证」。

已实现(本轮修复代码):

- **复审 P1**:`sanitize.py` 识别二次序列化层的转义引号值定界(边界为 `\"`;值内编码引号 `\\\"` 与编码反斜杠 `\\` 是内容),值内 `, ; }` 不再终止打码,兄弟字段不误伤;
- **复审 P2**:`search/keyword.py` `_finalize_hit()` 改用列级 select 直读数据库,绕过会话 identity map 陈旧实例;直接搜索与送入模型的共同路径覆盖;
- **复审 P3(一并修复)**:`AiAssistantPanel.vue` 检索请求序号 + 最近提交查询快照,过期响应不修改结果/error/loading;切换故障与卸载同步使旧检索失效;
- 冒烟脚本判据收紧(`backend/scripts/smoke_llm.py`):真实冒烟仅允许 `cloud`/`local`,其他模式明确拒绝且 0 次调用;JSON 能力关闭记「跳过/未验证」;JSON 项不剥离围栏、必须裸 JSON 对象;退出码不足以判断 JSON 已验证时以输出为准。

未验证(如实标注):

- **完整真实 `/api/faults/{id}/analyze` 端到端**(纯虚构问题/日志/测试知识走真实模型)按复核「下一步顺序」待独立复审通过后执行。
- local 模型、其他云服务仍未联调;浏览器闭环、Linux 云服务器部署、备份恢复演练、embedding/混合检索未验证。
- 本轮 `sanitize.py`/`keyword.py`/`smoke_llm.py` 改动已同步进运行中的后端容器验证;下次 `docker compose build backend` 重建镜像时自动包含。

### 阶段 2 真实联调与浏览器复审修复(2026-10-15,按《mimo-第二阶段真实联调与浏览器复审-2026-10-15.md》修复 P2-1/P2-2)

已实现(本轮修复代码):

- **P2-1 Schema 结构错误进入有限修复**(`backend/app/ai/`)：
  - 知识元数据改为显式整数版本(`source_type=sop, source_id=2, version=2`),不再用 ` v2` 展示后缀诱导字符串版本(`service.py` `_build_user_prompt`);系统提示增加 SOP `"version": 2` 示例、明确不能写 `"v2"`,并要求与知识片段清单一致(`prompts.py`);`KnowledgeReference.version` 描述同步收紧(`schema.py`)。
  - JSON 语法错误与 Schema 结构错误**共享一次修复预算**(`MAX_FORMAT_REPAIRS=1`):结构错误的修复反馈包含字段路径与期望类型(如 `references.0.version: 期望整数…`),修复请求继续声明 JSON 模式,逐次累计实际 usage/call_count,预算耗尽明确失败(`schema_mismatch`/`bad_response`);连续两次结构错误、语法错后结构错均不再多调用。
  - 版本严格校验不变:`version:"v2"` 直接 Schema 拒绝(不宽松转换为可信引用),修复成功后仍按后端片段的来源 ID/版本/状态复核,不一致即移除。
- **P2-2 failed 分析前端展示**(`frontend/src/views/AiAssistantPanel.vue`):
  - 业务 failed(新结果与历史统一)从分析记录派生展示:持久失败原因(`error_message`)、失败类型、人工重试入口;不再要求「有 report」或「无 analysis」,failed/report=null 不再空白;
  - 状态/调用次数/延迟/真实 usage 元信息独立于 report 展示;无 usage 显示「用量未知」;
  - 请求层失败保留上一份成功报告时明确标注「旧结果」,不冒充本次结果;HTTP 201 不再作为分析成功判据。

已验证(实际运行,均为本地 Windows + Docker Desktop):

- **后端 pytest 120 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,221.8s;含既有 113 项回归)。本轮新增 7 项(`tests/test_ai_analysis.py`)：
  - P2-1 主场景:首个响应合法 JSON/`version:"v1"`(字符串)→ 修复一次 → `version=1` 成功;共两次调用、修复仍为 JSON 模式、usage 正确求和(5+10/10+18)、引用仅在与片段版本一致时保留;
  - 修复反馈含字段路径(`references.0.version`)与期望类型;连续两次结构错误明确失败(`schema_mismatch`,2 次调用后放弃);语法错后结构错共享一次预算(不再有第三次调用);结构修复路径上 usage 缺失即未知;
  - 提示词元数据断言:`source_type=sop, source_id=2, version=2` 整数形式、无 ` v2` 后缀、系统提示含 `"version": 2`/`"version": null` 示例并声明不能写 `"v2"`;`version:"v2"` 直接 ValidationError、与片段版本不一致的引用移除(不宽松转换)。
- **前端 vitest 19 项全部通过**(`cd frontend && npm test`;含既有 16 项回归)。本轮新增 3 项(`frontend/tests/aiAssistantPanel.spec.ts`):
  - HTTP 201 的 failed/report=null(有 usage):持久显示失败原因/用量/调用次数/重试,输入保留,不空白;
  - 点击历史 failed:显示 `error_message`/用量未知/调用次数/重试(不只「历史报告」提示);
  - 请求失败保留上一份成功报告:标注「旧结果」且失败原因可见。
- **前端构建**:`vue-tsc -b && vite build` 通过(本机 Node 24 / npm 11;主包大小提示仍存在)。

未验证(如实标注):

- 修复后的**成功完整真实分析、补证浏览器流程、缓存与有效引用失效验证**待审查方按复审交付要求执行;真实修复路径(`version:"v2"` → 修复)仅经合成适配器回归,未再次调用真实模型。
- local 模型、其他云服务仍未联调;Linux 云服务器部署、备份恢复演练、embedding/混合检索(第三阶段)未验证。
- 浏览器人工走查本轮未重跑(组件行为由 vitest 覆盖);不启动第三阶段。

### 阶段 3 第一批:AI 辅助生成 SOP 草稿(2026-10-15,交回复审)

已实现(实际业务流程):

1. 案例详情(已通过案例)点击「AI 生成草稿」→ 后端校验生成权限(运维仅限自己提交;审核员 403;管理员不限)与案例状态(待审/退回 400,不调用模型);
2. 读取可信输入快照(人工确认根因/处理/恢复验证 + 故障描述/错误日志/近期变更证据,证据 ID 后端分配),调用当前配置模型生成独立 Schema 的 SOP 内容(9 个内容字段 + 待补信息 + 来源标注);
3. 校验来源标注(编造证据 ID 移除并标注)与缺失标注(材料缺失强制「待补/待人工确认」,推测性回滚强制「待验证」);模型返回后、创建草稿前重新读取来源状态与输入快照(列级),来源变化即失败不落草稿;
4. 成功创建新的 draft(`doc_id`/`version=1`/`status=draft`/`source_case_id`/创建者/维护人由后端决定),前端跳转 SOP 编辑页,人工核对补充后走既有提交/退回/审核/发布流程;不自动提交/发布,不覆盖已有 SOP,不改写案例与故障的人工确认结果;
5. 运行记录落独立表 `sop_ai_generation_runs`(来源案例、输入哈希/快照标识、配置版本、真/演示、状态、调用次数/延迟/真实 usage、待补信息、失败类型与安全原因),成功留审计 `sop.ai_generate`;幂等 `request_token` 防重复创建/重复计费,主动再次生成(新 token)可再建新草稿。

变更文件/接口/Schema/迁移:

- 新增 `backend/app/ai/json_out.py`(分析与 SOP 生成共用的 JSON 提取/结构错误反馈/usage 聚合/一次修复预算循环;分析编排改为复用,行为不变)、`backend/app/ai/sop_schema.py`(`SopDraftContent` 独立 Schema)、`backend/app/ai/sop_prompts.py`、`backend/app/ai/sop_draft.py`(编排:幂等/限流/来源复核/演示);
- `backend/app/models.py` + 迁移 `f7e8d9c0b1a2_add_sop_ai_generation_runs.py`(新表;status 用字符串列不引入 PG 枚举残留);
- 接口:`POST /api/cases/{id}/generate-sop-ai`(201 创建运行记录,业务成败看 `run.status`;400/403/404/409/429/502 按失败类型)、`GET /api/cases/{id}/sop-generations`(倒序;他人历史不下发 `request_token`);既有 `POST /api/cases/{id}/generate-sop` 模板生成保持不变;
- 前端:`frontend/src/views/CaseListView.vue`(模板/AI 入口区分、生成中防重、失败原因+同 token 重试、用量未知、云模式出网提示、切换案例过期防护)、`frontend/src/types.ts` 新增生成运行类型;导航改为 `router.push`(与助手面板一致)。

已实际运行(合成适配器,不调用付费真实模型;本地 Windows + Docker Desktop):

- **后端 pytest 142 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,141.7s;含既有 120 项回归与迁移升级-回滚-再升级)。新增 22 项(`tests/test_sop_ai_generate.py`),覆盖:成功生成来源绑定正确且不自动审核/发布、可信输入与证据 ID;待审/退回/越权/审核员拒绝且不调用模型不落记录;调用期间状态/内容变化不落有效草稿(`source_changed`);结构修复成功(两次调用/usage 求和/JSON 模式保持)、预算耗尽(语法错+结构错共享一次)、超时、usage 缺失未知;disabled 拒绝且模板生成可用、demo 标注零调用、LOCAL_ONLY 拒 cloud(502 无记录)、local 路径 trust_env=False;最终请求无合成凭据、注入文本仅作数据、模型不得指定 status/doc_id、编造来源移除;同 token 幂等复用/在途 409/失败重试原地更新/新 token 新草稿/失败无半成品;草稿受既有提交完整性与版本规则约束、token 对他人隐藏、推测性回滚标注。
- **前端 vitest 26 项全部通过**(`cd frontend && npm test`;含既有 19 项回归)。新增 7 项(`frontend/tests/caseAiSopGenerate.spec.ts`):入口名称区分与云模式出网提示、业务成功才跳转且用量可见、业务 failed 持久显示原因/用量/重试不跳转、重试同 token 与再次生成新 token、生成中防重复点击、切换案例过期响应不应用、disabled 禁用 AI 入口而模板可用。
- **前端构建**:`vue-tsc -b && vite build` 通过(主包大小提示仍存在)。

失败、去重、来源变化与模式限制的处理:

- 失败:模型/格式失败落 failed 运行记录(安全错误类型与脱敏原因),不创建任何 SOP 行;前端持久展示原因/调用次数/延迟/usage(缺失显示「用量未知」)与人工重试;页面输入不丢。
- 去重:`request_token` 唯一约束 + 运行记录状态;成功结果幂等返回(reused=true,不重复计费);生成中重复提交 409;失败重试同 token 原地更新(每个 token 至多一份草稿);过期 running 记录 15 分钟后允许接管,防进程崩溃卡死。
- 来源变化:调用前与创建草稿前各做一次列级来源读取(状态 + 内容哈希);变化则 `source_changed` 失败,结果不落为草稿。
- 模式限制:disabled 400(不调用模型)、demo 标注且用量未知、cloud/local 失败不回退、LOCAL_ONLY/代理隔离经 `build_adapter` 覆盖生成路径(含回归断言)。

尚未验证(如实标注):

- **真实模型联调**:本批不调用付费真实模型(合成适配器覆盖协议与失败形态);完整真实 AI 生成 SOP 联调由审查方后续执行,不沿用上一轮复审调用额度。demo/browser 端到端走查未在本轮重跑。
- 生成内容的真实质量(措辞整理是否忠实于来源、待补标注是否恰当)依赖人工核对,合成测试只验证结构/规则/权限,不验证真实模型文风;未做模型效果评测。
- 第二批(embedding 适配与索引管理)、第三批(混合检索与可靠性验证)未开始;Linux 部署、备份恢复演练仍未验证。

可重复演示步骤(演示模式,零真实模型调用):

1. `docker compose up --build -d` 并登录操作员账号(演示账号见 `.env.example`);
2. 故障 → 人工确认解决 → 审核员通过案例(或使用既有已通过案例);
3. 案例库打开该案例 → 点击「AI 生成草稿」(演示模式下结果标注「演示数据 / 模拟生成」,用量显示未知);
4. 进入生成的 SOP 草稿核对、补充 → 提交审核 → 审核员发布;全程无人工以外的自动提交/发布;
5. 同一案例再次点击「AI 生成草稿」会生成另一份新草稿;失败时面板显示原因与「重试(同一次请求)」。

### 阶段 3 第一批复审修复(2026-10-15,按《mimo-第三阶段第一批独立复审-2026-10-15.md》修复 P1-1/P2-1~P2-4)

已实现(修复内容):

- **P1-1 幂等返回归属/可见性统一复核**(`app/ai/sop_draft.py`):抽取 `_check_run_owner`/`_success_result`/`_conflict_or_reuse`,正常查询、并发首撞(IntegrityError)、领取失败等**所有既有结果返回路径**一致复核用户与案例归属,并复核草稿可见性;不匹配明确 409,不向响应序列化他人的 run/SOP。
- **P2-1 数据库原子领取与条件提交**:`_claim_run` 以 CAS 条件更新(仅 failed/过期 running 可领取,`attempt+1` 并提交)保证并发只有一个执行者;`_finalize_run` 按 `id+attempt+running` 条件提交结果,被接管后旧执行者的草稿整体 rollback 丢弃(不留下孤儿草稿)、不覆盖新结果,返回可识别冲突(或已完成结果);过期 running(>15 分钟)允许接管,防进程崩溃卡死。
- **P2-2 卸载副作用保护**(`CaseListView.vue`):`disposed` + 卸载时序号整体失效;成功/失败/finally/通知/跳转全部守卫,迟到响应不再强制跳转或弹旧组件通知;切换案例保护保持。
- **P2-3 重试绑定本次输入/配置**:领取时刷新 `input_hash`/`input_snapshot`/`model_config_version`/`is_demo` 为本次执行的实际值;记录语义明确为「最近一次执行尝试的输入与配置」(`attempt` 标识执行次数),不再出现 demo 标识与旧配置并存的矛盾。
- **P2-4 字段来源映射持久化**:`verify_draft_content` 清洗后的映射存入运行记录新列 `field_sources` 并随 API 返回;写入草稿 notes「字段来源依据」块(刷新/重新进入草稿仍可核对);无有效来源映射的关键内容强制「待人工确认」并记录校验原因(校验提示一并写入 notes);映射只含证据 ID 标识,不携带来源正文。`sop_ai_generation_runs` 表新增 `attempt`/`field_sources` 列(迁移 `f7e8d9c0b1a2` 同步更新,尚未对外发布)。

已实际运行(合成适配器,不调用付费真实模型):

- **后端 pytest 149 项全部通过**(`MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps backend python -m pytest tests/ -q`,157s;含既有 142 项回归)。新增 7 项(`tests/test_sop_ai_generate_review.py`):并发首撞 IntegrityError 分支不返回他人私有草稿(409,他人幂等不受影响)、顺序重放他人 token 冲突、独立会话并发领取至多一个执行者、过期接管后旧执行者草稿整体丢弃(无孤儿草稿/attempt=2 可见)、重试绑定本次配置与输入(attempt=2)、字段来源映射持久化(响应/记录/notes 可核对、伪造 ID 移除、不泄露材料正文)、无来源关键内容标注待人工确认。
- **前端 vitest 29 项全部通过**(`cd frontend && npm test`;含既有 26 项回归)。新增 3 项:卸载后迟到成功响应不跳转不弹通知、卸载后迟到失败响应无副作用、成功结果展示字段来源依据。
- **前端构建**:`vue-tsc -b && vite build` 通过。

尚未验证(如实标注):

- 复审要求的真实并发 HTTP 探针(双操作员屏障碰撞)以独立会话单元/集成回归覆盖等价路径,浏览器迟到响应以组件测试覆盖;真实浏览器复验与本批真实模型联调待审查方执行。
- 其余与第一批交付相同:真实模型质量、local 模型、第二/三批、Linux 部署、备份恢复未验证。

### 阶段 3 第三批:向量检索、混合检索与可靠性(2026-10-15,交回复审)

已实现:见上一节「向量检索、混合检索与可靠性(第三阶段第三批)」。变更文件:

- 新增 `backend/app/search/hybrid.py`(向量候选 + RRF 融合 + 统一入口/降级);
  `backend/app/search/keyword.py` 抽出共用 `finalize_hits`/`source_display_fields`;
- `backend/app/routers/ai.py`(`/api/search` 走混合入口)、`backend/app/ai/service.py`
  (助手共用同一检索与来源校验;缓存键并入检索版本)、`backend/app/schemas.py`/`types.ts`
  (响应增 `retrieval_mode`/`degraded`/`degrade_reason`,旧字段兼容);
- 前端 `AiAssistantPanel.vue`:实际检索方式标签、降级原因展示、cloud/local 出网说明;
- 评测:`backend/scripts/eval_retrieval.py`(独立库 `opssop_eval`,五类查询集 + Recall@3/5/
  MRR/空结果正确率口径,`--setup/--synthetic/--cleanup`);
- 测试:新增 `tests/test_hybrid_search.py`(20 项)。

已实际运行(合成向量;真实模型调用 **0** 次;本地 Windows + Docker Desktop):

- **后端 pytest 214 项全部通过**(含既有 194 项回归;225s)。20 项覆盖:
  disabled/未构建/指纹不兼容零调用降级、超时/429/非法响应/维度异常降级不丢查询、
  local-only 拒云路径(构造即拒零调用)、local 失败只有限重试不切服务、查询最终请求脱敏、
  改写问法经向量命中并去重、top_k 上限与稳定排序、数据库距离排序近邻在前、
  关键词与向量双路排除待审/草稿/废弃来源(含 finalize 独立断言)、索引未更新时失效来源
  不可用(搜索与助手一致)、内容变化旧向量剔除、草稿不替换已发布/换版后仅新版本为依据、
  检索窗口内来源失效不返回、代次切换重读不混用、检索策略/代次变化使分析缓存失效。
- **前端 vitest 31 项全部通过**(含既有 29 项);新增 2 项:混合方式与降级原因展示、
  服务错误不显示为无匹配;`vue-tsc -b && vite build` 通过。
- **评测(合成向量,独立库)**:关键词 Recall@3=0.75 / MRR=0.75;混合 Recall@3=1.0 /
  MRR=0.875;空结果正确率均 1.0(无答案/来源失效零误报)。E1(问法改写)关键词不中、
  混合经向量入前 3——**以上只验证脚本、排序机制与统计口径,不代表真实语义质量或
  Recall 提升;真实效果保留待运行状态**。
- **真实浏览器验证(应用 API + 实际页面;模型为合成服务)**:3 项场景通过——
  ① 正常搜索(页面/API 均为混合方式)与来源跳转(`/sops/2`);② 停掉合成 embedding 服务后
  显示「已降级为关键词检索」且降级原因(`查询向量生成失败(connect)`)与关键词结果可见,
  不冒充混合;③ 迟到检索响应(route 暂扣后释放)不覆盖新查询。证据:仓库外
  `mimo-batch3-verification/`(browser-result.json、screenshots/、合成服务与脚本)。

权限、陈旧索引、代次切换、引用失效与倒序响应的具体证据:见上述测试项与既有回归
(引用失效=第二批 `test_references_revalidated_after_model_call` 与缓存复核;倒序/切换/卸载
=前端 searchStaleness 六项);并发以独立会话 + 屏障/钩子窗口控制,未把顺序调用称为并发。

尚未验证(如实标注):

- **真实 embedding/聊天模型联调**:未调用;合成向量不证明语义检索准确率、Recall 或模型能力;
  真实效果评测待配置真实服务后去掉 `--synthetic` 运行。
- local 真实模型、其他云服务、Linux 云部署、备份恢复未验证;第四阶段全面交付/部署未启动。

### 阶段 3 第三批修复:独立复审 5 项 P2(2026-10-16,交回修复复审)

针对第三批独立复审的 P2-1~P2-5 与两项较低优先级问题,按序修复(真实模型调用 **0** 次):

- **P2-1 降级出口绕过来源复核**:`hybrid.py` 全部返回出口(成功/限流/配置/维度/失败降级)
  统一经 `finalize_hits` 复核;`service.py` 新增 `_revalidate_snippets`,**模型请求发出前**
  按当前状态/版本重取片段或剔除失效内容(失效知识不发给模型)。
- **P2-2 向量候选先 LIMIT 后过滤**:`_vector_distance_query` SQL 前置过滤状态/权限/有效版本
  (废弃/换版/无权近邻不进候选池);`_fetch_vector_hits` 对内容指纹陈旧候选**有界补取**
  (窗口 20→40→…,单请求上限 `MAX_VECTOR_FETCH=60` 行)。
- **P2-3 embedding 与聊天共用限流桶**:`_acquire_embed_slot`/`_release_embed_slot` 独立配额
  (`EMBEDDING_RATE_LIMIT_PER_MINUTE=30`/`EMBEDDING_MAX_CONCURRENT_PER_USER=4`,已入
  `.env.example`/compose);检索不占聊天额度,缓存命中不占聊天额度,向量限流单独降级并解释。
- **P2-4 状态未知仍外发 embedding 查询**:`GET /api/search` 增 `keyword_only` 服务端纯关键词
  路径(零 embedding 调用);前端 aiStatus=null 时只走该路径并提示「本次仅关键词检索(查询
  文本未发送)」,状态确认后混合检索与出网说明一致;补浏览器场景与组件测试。
- **P2-5 真实评测入口未构建真实索引/降级汇总成混合指标**:`prepare_index` 对真实/合成入口
  都**强制重建**对应来源索引(真实入口严格走配置适配器,来源不混用);存在降级时
  `summary.hybrid` 标 `unverified` 并给 `degrade_rate`,退出状态 3;构建/配置错误退出 2;
  空结果正确率按真实返回计误报(任何命中均计);`--setup` 参数真实生效(缺夹具要求先 setup)。
  另:新检索失败后旧方式标签/旧降级横幅不再残留。

已实际运行(合成向量;真实模型调用 **0** 次):

- **后端 pytest 233 项全部通过**(214 既有 + 19 新增;330s)。新增覆盖:两个 P2-1 窗口探针
  (降级出口复核、失效知识不进聊天请求)与模型请求前片段复核;P2-2 废弃近邻不挤占 +
  陈旧候选有界补取(含窗口上限断言);P2-3 聊天额度 1 首次分析成功/搜索不占聊天额度/
  embedding 独立限流/缓存命中不占额度/并发释放;P2-4 keyword_only 零调用;P2-5 评测契约
  (全降级/部分降级→unverified+exit 3、全 hybrid→数值+exit 0、真实入口构建真实索引、
  构建失败/未配置可识别错误、空结果误报口径、--setup 生效)。
- **前端 vitest 34 项全部通过**(31 既有 + 3 新增:状态未知 keyword_only 请求与提示、
  cloud 状态一致出网说明、失败后旧标签/横幅清除);`vue-tsc -b && vite build` 通过。
- **评测脚本复跑(合成向量,独立库)**:exit 0、`hybrid_metrics_valid=true`、
  `index_source=synthetic-eval` 强制重建(5 单元);指标与此前一致(关键词 0.75/0.75,
  混合 1.0/0.875,空结果正确率 1.0——仅机制演示)。真实入口未配置演示:exit 2 明确报错。
- **真实浏览器 4/4 通过**(应用 API + 实际页面;合成 embedding 服务带调用计数):
  ① 混合搜索+来源跳转 `/sops/2`;② embedding 断开降级横幅+原因+关键词结果;
  ③ 迟到响应不覆盖新查询;④ **状态接口 503 时检索走纯关键词路径,页面提示
  「本次仅关键词检索/查询文本未发送」,合成服务调用计数不变(零外发),关键词结果正常**。
  证据:`mimo-batch3-verification/`(browser-result.json、screenshots/s4-status-unknown.png、
  eval-fix-report.json、eval-real-noconfig-stderr.txt)。
  (验证环境注:用户另一套 deploy 栈占用 8080,前端验证容器临时映射 8081 运行,未操作
  deploy 容器;验证后已删除。)

尚未验证(如实标注):真实 embedding/聊天模型联调与真实语义效果仍为 0 调用待运行;
local 真实模型、其他云服务、Linux 云部署、备份恢复未验证;第四阶段未启动。
**未宣称第三阶段整体验收通过**,待第三批修复独立复审。

### 阶段 3 第三批修复 2:发模型前片段保留查询相关依据(2026-10-16,交回修复复审)

针对修复复审新增 P2-6(发模型前复核覆盖命中片段,合法诊断依据被丢弃)修复
(真实模型调用 **0** 次;原 5 项 P2 修复保留不动):

- `keyword.py`:新增 `source_snippet_for_query`(与正式检索共用提取逻辑:`extract_terms`/
  `_score_text`/`_make_snippet`/字段权重,及抽出的 `_best_field_hit`);**旧命中片段仍可在
  当前来源验证则保留其命中位置**,来源变化后按原查询在当前字段重新定位,不再把片段
  统一替换为 symptom 前缀;来源读取用**列级直读**(`_read_source_fields`,绕过会话
  identity map 陈旧实例,内容变更窗口内可见);
- `_best_field_hit` 最佳字段选取由「第一个出现命中的字段」改为**加权得分最高的字段**:
  多词查询下弱词命中标题不再掩盖错误码等强词命中排查/处理字段(正式检索与发模型前
  复核共用,展示依据与助手依据不分叉);`search_knowledge` 两处打分循环改为共用助手;
- `service.py` `_revalidate_snippets` 接收原查询并调用 `source_snippet_for_query`;
  P2-1 的失效剔除行为不变;片段长度(`MAX_SNIPPET_CHARS=800`)与最终脱敏约束保持。

已实际运行(合成向量;真实模型调用 **0** 次):

- **后端 pytest 238 项全部通过**(233 既有 + 5 新增;307s)。新增:复审复现场景
  (SOP 仅排查步骤命中,聊天请求保留「队列偏移量」类排查依据)、只命中 SOP 处理字段、
  只命中案例人工根因字段、来源内容变化后采用当前内容不沿用旧正文、片段长度上限;
  废弃/换版剔除回归(P2-1)保持通过。
- **前端 vitest 34 项通过**;`vue-tsc -b && vite build` 通过(本轮前端无改动,复跑确认)。
- 复审探针 `test_revalidation_preserves_query_matching_sop_instruction` 的等价断言
  已由上述新增回归覆盖并通过。

尚未验证(如实标注):真实 embedding/聊天模型联调与真实语义效果仍为 0 调用待运行;
local 真实模型、其他云服务、Linux 云部署、备份恢复未验证;第四阶段未启动。
**未宣称第三阶段整体验收通过**,待修复复审。

### 阶段 3 第三批修复 3:embedding 429 不立即重发(2026-10-19,按独立核实报告)

针对《OpsSOP-第三阶段独立核实结果-2026-10-19》新增 P2(上游 429 后立即重试:混合
检索查询向量与索引批量嵌入两条路径均实际复现,两次请求间隔 <1ms)修复(真实模型调用
**0** 次):

- `embeddings.py`:429 映射为 `rate_limited` 且**非 retryable**;模块新增
  `RETRY_BACKOFF_SECONDS=0.5`,仅超时/连接/5xx 的一次有限重试使用,避免零延迟重发;
  限流响应不解析 `Retry-After`、不换服务;
- `hybrid.py`:查询向量遇 429 直接降级关键词(**只发一次请求**);其他可重试错误退避
  0.5s 后重试一次,仍失败即降级;
- `embedding_index.py`:批量嵌入遇 429 直接失败(**只发一次请求**,`embedded_calls=1`),
  移除 `sleep(0)` 的同批次重发;超时/连接/5xx 仍有一次带退避重试;
- 回归:新增两项真实适配器错误映射用例(替换 `httpx.Client` 返回 429,**断言实际 POST
  次数为 1**),分别覆盖 `/api/search` 降级与索引构建失败;既有超时/连接有限重试用例保持;
- `scripts/browser_e2e.py`:步骤 5 的按钮定位由旧名「生成 SOP 草稿」改为页面现有
  「模板生成草稿」(源码级修正;本轮未运行浏览器脚本)。

已实际运行(合成向量;真实模型调用 **0** 次;本地 Windows + Docker Desktop):

- **后端 pytest 240 项全部通过**(238 既有 + 2 新增;256.5s;含权限、状态、迁移、AI、
  索引与检索回归;1 条 Starlette/AnyIO 弃用警告,与本修复无关)。
- 定向复跑:embedding/混合检索/索引 67 项通过;两条 429 路径均断言只有一次请求。
- 未运行:独立核实方的探针原文件(其源码与证据目录不在本仓库),也未重复其前端/浏览器
  检查;真实 embedding 服务、Linux 云部署与备份恢复仍未验证。

### 阶段 4 第一批:来源定位、显式演示内容与 demo 浏览器闭环(2026-10-21,交回复审)

本批在已有代码上增量实现,未重写模型编排/检索算法,未引入新任务框架。真实模型调用 **0** 次;
所有验证均在隔离的本地环境完成,未复用或清理 `mimo_pgdata`、未修改真实 `.env` 与业务库。

实现内容:

- **来源定位**:新增只读 `/cases/:id` 直达页;助手搜索命中与报告引用统一提供「查看案例/查看 SOP」,
  地址只由受控 `source_type` + 正整数 `source_id` + 版本展示构造(不使用模型返回 URL);新标签页打开,
  保留左侧未保存输入;登录 `redirect` 回跳(仅站内路径);403/404/非法 ID/网络错误分别展示;
  SOP 历史/废弃状态提示保留;案例入口不返回来源故障的原始日志/检查记录/生成历史。
- **显式演示内容**:新增 `demo_seed_records` 稳定标识表与 Alembic 迁移、`app/demo_content.py`、
  `backend/scripts/seed_demo_content.py`;18 个场景(9 正向 + 6 负面夹具 + 3 边界),单事务、幂等、
  production 拒绝、不覆盖人工修改;提示词注入文本只作为待分析数据保存,不执行命令或改变权限。
- **demo 浏览器闭环**:新增 `scripts/demo_browser_e2e.py`,真实页面 + 业务 API,覆盖创建故障、
  demo 分析、失败注入、来源定位、补证再分析、人工确认、案例审核、AI 生成草稿、人工补全提交、
  审核发布、正式检索定位、负面来源排除、错误状态、登录回跳与可选跨账号权限断言;输出 JSON/截图。

实际运行结果(本地 Windows 11 + Docker Desktop;本修复轮使用独立 Compose 项目
`opssop-demo-p4b1`、独立卷 `opssop-demo-p4b1_pgdata`、端口 `18000/18080/15432`;
后端使用现有 review 镜像挂载当前源码;`AI_MODE=demo`、`EMBEDDING_MODE=disabled`、`LOCAL_ONLY=1`):

- **后端 pytest 253 项全部通过**(405.17s;240 项既有 + 13 项本批新增;含权限、状态、迁移、
  演示 seed 幂等/回滚/负面夹具;1 条 Starlette/AnyIO 弃用警告,与本批无关)。
- **迁移测试 2 项通过**(2.64s):空库升级到 head、升级-回滚-再升级;`demo_seed_records` 已包含。
- **前端 vitest 49 项全部通过**;`vue-tsc -b` 类型检查与 `vite build` 生产构建通过。
- **演示 seed**(实际 CLI):首次 `created={fault:18, case:15, sop:14}`;重复执行
  `created={fault:0, case:0, sop:0}`、`skipped={fault:18, case:15, sop:14}`;
  `--list` 列出 18 个场景;`OPSSOP_ENV=production` 拒绝执行并返回退出码 2。
- **demo 浏览器闭环**:运行标记 `[demo-e2e:572d3affec]`,**86 项通过 / 0 失败 / 0 跳过**;
  JSON 结果 `scripts/e2e-artifacts/572d3affec/result.json`(本地产物,已 Git 忽略)。
  覆盖 demo `call_count=0`/usage 未知、关键词检索标注、根因人工填写不被 AI 覆盖、草稿不自动发布、
  负面来源检索排除、历史版本提示、登录回跳与跨账号 403。
- **第一阶段浏览器回归**:`scripts/browser_e2e.py` 在 18080 生产预览上 **25 项全部通过**
  (运行标记 `[br:fb5e187d]`);修复了步骤 9b 在页面加载完成前统计按钮的竞态
  (改为等待 h1 标题出现,不再以固定 sleep 作为成功判据)。

复审修复(2026-10-22,按《OpsSOP-第四阶段第一批独立复审-2026-10-22.md》):

- **P1 Compose 隔离**:`docker-compose.yml` 后端宿主端口改为可覆盖的 `BACKEND_PORT`,
  前端增加 `FRONTEND_HOST`;README 的初始化/seed/检查/清理命令统一使用
  `-p opssop-demo-p4b1 --env-file .env.demo` 与独立端口 `18000/18080/15432`。
  `docker compose config` 实测项目名 `opssop-demo-p4b1`、卷 `opssop-demo-p4b1_pgdata`,
  未触碰 `mimo_pgdata`。
- **P2 忽略本地配置**:`.gitignore` 增加 `.env.*` 并保留 `!.env.example`;
  `git check-ignore` 实测 `.env.demo/.env.local/.env.production` 被忽略、`.env.example` 仍可跟踪。
- **P3 失败注入证据**:分析失败前写入与已保存值完全不同的描述/日志,失败后精确比较两字段并
  GET 校验数据库仍为原值;补证失败等待按钮恢复和错误提示后比较,并校验无新增记录、表单未写入数据库;
  负面检索前增加夹具存在及状态前置检查。加强后的完整闭环 86 项全部通过。

尚未验证(如实标注):真实云端/本地聊天模型质量、真实 embedding 与语义检索效果、Linux 云服务器
与 HTTPS 部署、备份恢复演练、GPU 相关能力;演示 seed 与 demo 浏览器闭环均只代表隔离本地环境,
不代表第四阶段整体验收通过。本批改动尚未 commit/push,交回独立复审。

### 阶段 4 第二批:版本化评测集、检索/分析评测与机器可读报告(2026-10-22,复审修复后交回复审)

本批新增合成评测工具,未改前端业务页面,未调用真实聊天/embedding 模型,未读取真实模型配置;
第一批(来源定位、演示内容、demo 浏览器闭环)已于 2026-10-22 通过独立验收,本批不重做。

实现内容:

- `backend/eval_data/v1/`:知识夹具、样本、参考标签、合成模型行为分开存储;39 条样本、25 条知识夹具、
  数据集版本 `2026-10-22.1`;稳定键 `kb:*`,标签状态 `provisional`、`human_reviewed=false`。
- `backend/app/eval/`:数据集加载/校验、数据库隔离与 manifest、夹具准备、指标数学、合成聊天/embedding
  适配器、检索运行器、分析运行器;复用真实 `GET /api/search` 与 `POST /api/faults/{id}/analyze`。
- `backend/scripts/eval_suite.py`:主入口;每轮创建 `opssop_eval_<run_id>` 专用库,正式 Alembic 迁移,
  `eval_run_manifest` 记录归属/数据集哈希/夹具映射;连接前拒绝业务/测试/demo/系统库与 production;
  默认成功后按 manifest 精确删除本轮库。
- `backend/scripts/eval_retrieval.py`:旧入口保留 0/2/3 退出语义与旧 6 查询契约;修复固定目标库/
  create_all/标题前缀清理,改用每轮安全库、标签清理和精确 `--cleanup --target-db/--run-id`;
  真实 embedding 旧入口需显式 `--allow-real-embedding`。
- 分析评测覆盖分类/弃答/失败分母、原始引用(清洗前)与最终引用(清洗后)分开统计、格式修复初次/
  采用输出、无效引用、部分 usage、调用期间来源失效、缓存复用零新增调用、无权 403、注入文本无副作用。
- 检索评测覆盖 Recall@3/5、MRR、无答案正确率、按分组计数、SOP 版本匹配、重复片段去重、混合降级
  整批标 unverified、关键词指标仍可读。
- 新增 36 项评测测试(数据集/数据库隔离/指标/合成适配器/混合降级/完整集成),既有 253 项后端测试保留。

已实际运行(2026-10-22,本地 Windows + Docker Desktop;隔离 PG16/pgvector 容器;review 镜像挂载当前
`backend/` 源码;合成适配器、零真实模型/embedding 调用):

- **后端全量 289 passed**(253 既有 + 36 新增;200.42s;1 条 Starlette/AnyIO 弃用提示)。
  - 说明:首次以 `LOCAL_ONLY=1` 运行全量时有 5 项既有云端 embedding 测试失败(环境变量冲突,
    不是本批代码回归);按既有测试环境改用 `LOCAL_ONLY=0` 后 289 项全部通过。评测入口自身使用
    显式合成设置,不受该环境变量影响。
- **最终合成评测** `[evalb2a1b2c3da]`:39 样本;检索 24/24、分析 32/32;退出码 **0**;
  本轮评测库 `opssop_eval_evalb2a1b2c3da` 已按 manifest 自动删除。
  - 报告与摘要:运行输出目录 `scripts/eval-artifacts/<run_id>/`(Git 忽略;本批实际产物保留在仓库外)。
  - 合成机制结果:关键词 Recall@3 0.9667(14.5/15)、Recall@5 1.0、MRR 0.9222、无答案 7/9;
    合成混合 Recall@3 0.9111、Recall@5 0.9667、MRR 0.9133、无答案 6/9;0/24 降级。
  - 分析主分类 18/18、有效建议覆盖 18/18、应弃答 3/3、主 suite 失败 0/21;主引用采用输出
    60/60 有效;失败注入引用 12/13 有效、不可观察 4、格式修复 3 例(1 成功/2 失败);
    缓存复用请求新增调用 0;无权分析 403;注入无用户/审计命令副作用。12/12 机制断言通过。
  - 以上数值来自合成适配器与合成参考标签,不是真实模型质量、真实 embedding 效果或真人审核结论。
- 前端:本批未改前端业务代码;沿用第一批独立验收的 49 项测试与构建通过结果,未重复运行浏览器闭环。

未验证(如实标注):真实聊天模型质量、真实 embedding/语义检索效果、Linux/HTTPS 部署、备份恢复演练;
参考标签未经真人复核,真实人工修改比例不可用;合成评测通过不代表第四阶段整体完成。本批改动尚未
commit/push,交回独立复审。

#### 阶段 4 第二批独立复审修复与再修复(2026-10-22)

按《OpsSOP-第四阶段第二批独立复审-2026-10-22》修复 7 项 P2,未重做第一批,未改真实业务库/配置:

- **引用分母**:畸形/非对象引用、缺少 ref_id、未知 ID、类型/版本不匹配全部进入分母;
  `valid + invalid == occurrences` 并报告 `denominator_consistent`;覆盖初次、修复/采用、混合与全部畸形。
- **敏感元数据**:不可信 ref_id 不再原样进入报告,只记录类型、长度、SHA-256 前缀;
  refs/distinct/valid/invalid 与错误摘要统一使用受控标识,新增敏感标记、超长、非字符串回归。
- **最终引用有效性**:`references.*.final` 新增 valid/invalid 出现次数、可观察样本、
  `validity` 比值与复核依据;逐条按数据库 case=approved / SOP=published 且版本匹配复核,
  没有报告/无法观察时明确 `not_applicable`,不再从清洗后总数推断全部有效。
- **报告写入失败**:摘要/JSON/输出目录任一写入失败均退出 4,stdout 同步给出 `write_errors`;
  先写摘要再写 JSON,尽量保留可用产物,不把产物未交付写成成功。
- **中断部分报告**:检索/分析逐条捕获中断;已完成行、计划/执行/未执行、失败样本和
  `complete=false`/`partial` 状态写入部分 JSON;CLI 对运行器异常也生成部分报告。
- **迁移失败孤儿库**:只对本轮 `CREATE` 的库在 Alembic/manifest 失败后按精确名称清理,
  清理结果写入 `setup_cleanup`;陌生既有库仍按 manifest 防护拒绝接管/删除。
- **缓存来源复核**:缓存建立后实际废弃 SOP,再通过真实 `POST /api/faults/{id}/analyze`
  观察结果;区分 `cache_hit_revalidated` 与 `cache_invalidated_reanalysis`,统计新增调用;
  无有效 SOP 引用时记 skipped/前置失败,不再把 `not_applicable` 算作通过。
- **CLI 边界**:外置合法数据集以 `external:<name>:<hash>` 标识,不写绝对路径,摘要不崩溃;
  非法 `run_id` 返回配置错误 2 并生成部分报告;数据集未加载时 JSON/摘要仍可安全生成。
- **最终引用身份一致性**(修复复审再发现):`ref_id` 编码的 type/ID/version 必须与显式
  `source_type/source_id/version` 一致;类型、ID、编码版本与字段版本冲突直接判无效,
  不允许 fallback 覆盖后仍算有效。
- **用户取消(KeyboardInterrupt)**:检索/分析在逐条边界捕获可处理取消,保留已完成行、
  取消位置和计划/执行/未执行;`cancelled=true`、`complete=false`,CLI 以 **130** 退出;
  主入口写部分报告并清理后返回 130;迁移被取消时先按精确名称清理本轮新建库再传播。
- **文档修正**:交付报告 PowerShell 示例移除 `MSYS_NO_PATHCONV=1` 赋值写法。

复审修复后实际运行(2026-10-22,同一隔离 PG16/pgvector;review 镜像挂载当前 `backend/`;零真实调用):

- **后端全量 312 passed**(201.94s;289 既有 + 23 项复审回归;1 条 Starlette/AnyIO 弃用提示)。
- **完整合成评测 `[reviewfixrun7]`**:39 样本;检索 24/24、分析 32/32;退出码 **0**;
  本轮评测库 `opssop_eval_reviewfixrun7` 已按 manifest 删除;12/12 机制断言通过(0 failed/0 skipped)。
  - 关键词 Recall@3 0.9667(14.5/15)、Recall@5 1.0、MRR 0.9222、无答案 7/9;
    合成混合 Recall@3 0.9111、Recall@5 0.9667、MRR 0.9133、无答案 6/9;0/24 降级。
  - 主分类 18/18、有效建议 18/18、应弃答 3/3;主引用原始 60/60、最终 60/60(21/21 可观察);
    失败注入原始 12/13、最终 11/11(4 个不可观察);格式修复 3 例(1 成功/2 失败)。
  - 缓存第二次复用新增调用 0;来源探测实际 API 请求 1 次,路径
    `cache_invalidated_reanalysis`,失效 SOP 引用未返回;新增调用 1 次单独计入 `cache_probe`。
- **独立复审探针**:原 8 项 **8 passed**;新增 4 项残留边界探针 **4 passed**
  (类型/版本身份冲突、取消进度、取消迁移清理)。
- **真实 CLI 边界**:输出路径为文件 → 退出 4 且评测库已清理;外置同内容数据集 → 退出 0;
  非法 run_id → 退出 2 并生成部分报告;普通中断检索 → 退出 4、已完成 1/24、未执行 23;
  用户取消检索 → 退出 **130**、`cancelled=true`、已完成 1/24、未执行 23、评测库已清理。
- 前端未改业务代码;沿用第一批独立验收的 49 项测试与构建结果,未重复浏览器闭环。

### 阶段 4 第三批:备份恢复、命名卷持久化与生产配置(2026-10-22,交回复审)

本批新增运维工具、生产 Compose/预检与操作文档,未扩展业务功能、未引入新服务:

- `backend/app/ops/`:标准库实现的备份/恢复/快照/演练核心;`backend/scripts/ops_cli.py` 为统一入口。
  - 备份 `pg_dump -Fc` + 元信息 + SHA-256;二进制只走 `docker cp`;
  - 恢复只允许新建目标库,先校验元信息/大小/SHA/归档可读性/版本/目标存在性,再创建;
  - 取消契约 130;失败目标默认保留,显式 `--cleanup-failed-target` 才清理本轮创建的目标。
- `docker-compose.prod.yml`:生产覆盖使用 `!reset`/`!override`,移除 DB/后端宿主端口与 tests 挂载,
  前端只绑定 `127.0.0.1`;`.env.production.example` 仅占位符。
- `backend/scripts/preflight.py` + `start.sh`:配置预检 → Alembic → seed/账号检查 → API;
  弱口令/演示开关在迁移前失败。
- `docker-compose.drill.yml`:隔离演练使用已有依赖镜像 + 当前源码挂载、新建命名卷/网络/
  独立端口,资源带 `com.opssop.run_id` / `purpose=drill` 标签。
- `docs/operations.md`、`docs/backup-restore.md`、`docs/deployment.md`:PowerShell 7 与 Linux
  命令、目标保护、角色/权限、升级回退、HTTPS/网络边界和验证矩阵。

实际运行(2026-10-22,本地 Windows Docker Desktop 的 Linux 容器;非云服务器/公网 HTTPS):

- **隔离演练 `[drill20261022h]` 全部步骤成功**:源库 A 正式迁移、全部业务夹具走 HTTP API、
  备份 A、恢复到全新 B、B 实例真实 HTTP 登录/检索/权限/续办、源库未变化。
  - 备份:54,000 bytes,sha256 `88f48bd1…`,Alembic `d4c5b6a7c8e9`,pgvector `0.8.7`;
  - 恢复:目标新库创建成功,计数/状态/业务摘要/Alembic 比较 **equal=true**;
  - 恢复后续办:新故障/补证/确认/审核成功,新 ID 继续分配,审计增加,源库快照不变;
  - 命名卷:正常重启后数据仍在;删除 DB 容器后用同一卷重建,容器 ID 变化
    (`896aaec0…` → `57d48478…`),标记/迁移版本/HTTP 登录仍在,并继续写入新记录;
  - 生产:合并配置无 DB/后端端口、无 tests 挂载、前端仅 127.0.0.1;空库仅 admin(1 用户、
    0 demo、0 业务),nginx 静态/SPA/`/api/` 健康与登录可用;
  - 负例:弱口令/演示开关退出 2 且未创建 `alembic_version`;遗留演示账号生产启动退出 1;
    受限角色迁移失败退出 1;全部本轮资源按标签清理(4 个负例库/角色/两个 Compose 项目/卷/网络)。
- **独立备份/恢复 CLI**:最小合成库备份 51,079 bytes/sha256 `0531d0f1…`,恢复到新库成功;
  再次指向已有目标被拒绝(退出 2);篡改归档被大小/SHA 预检拒绝(退出 2),未创建目标。
- **后端全量 329 passed**(312 既有 + 17 项本批运维测试;203.75s;1 条既有弃用提示)。
- **第二批回归**:`eval_suite --suite all --mode synthetic` 39 样本、检索 24/24、分析 32/32、
  退出 0、评测库已清理。
- **前端**:`npm test` 49 passed;`npm run build` 通过(未改前端/nginx 行为,浏览器闭环未重复)。

### 阶段 4 第三批复审修复(2026-10-22,交回独立复审)

针对独立复审的 2 项 P1、3 项 P2 修正保护与可靠性缺口,未扩展业务功能:

- 演练 Compose 的配置解析/启动使用受控子进程环境,剔除宿主同名插值变量和 `COMPOSE_*` 覆盖;
  首次 `up` 前核对最终数据库/挂载/模型禁用/端点为空/网络/端口,并硬校验项目/卷/网络均全新。
- 记录本轮容器/卷/网络清单及 ID;清理同时核对项目、用途、`run_id` 和确切名称/ID,
  区分 `cleaned`/`retained`/`not_found`/`nothing_to_clean`/`failed`,拒绝或非零不再汇总为成功。
- 备份/恢复的容器临时文件清理失败时 `success=false`、`status=cleanup_incomplete`,CLI 非零。
- 恢复在创建目标库前校验元信息完整结构及 `source_snapshot` 计数/状态/摘要/表清单;
  缺失或畸形不会创建/恢复目标。
- 演练准备阶段(环境文件、镜像、Compose 配置)失败也写非成功部分报告:当前步骤 failed、
  未执行步骤 `not_run`,并记录原因;报告写入失败显式非零。
- 独立复审 10 项探针 **10 passed**;新增 20 项回归;后端全量 **349 passed**(1 条既有弃用提示)。
- 修复后完整演练 `[fixdrill20261022g]` **20/20 步骤成功**(复用已构建前端 dist):
  备份 54,032 bytes、sha256 `fc29071a…`,恢复到新库 **equal=true**,同卷重建容器 ID
  `d396f0b9…`→`786fb84d…` 后数据和 HTTP 续写仍在;生产正例/4 类负例通过;清理 `cleaned`。
- 异属旧卷反例:CLI 在启动前退出 2,无任何本轮容器创建,旧卷未被修改,部分报告写入
  `baseline=failed`、`source_up=not_run`、`cleanup=nothing_to_clean`。
- 第二批合成评测回归 39 样本、检索 24/24、分析 32/32、退出 0、评测库已清理。
- 演练网络仍未启用 Docker `internal` 出口隔离(实测会阻断宿主访问已发布端口,而演练需要从宿主
  做真实 HTTP 检查),README 不把它声称为网络层保证;通过受控环境、禁用的模型配置和空端点
  约束外发。改动未 commit/push。

未验证/限制:真实云服务器、公网 HTTPS/证书链、真实 DNS/防火墙;真实聊天模型/embedding、
真人标签;从零构建新镜像;强制终止/断电自动恢复、自动异地容灾/调度。演练是“本地 Linux 容器
+ 当前源码挂载”验证,不是新镜像构建成功或目标云环境验收。本批改动未 commit/push。

### 阶段 4 第三批复审再修复(2026-10-26,已复验)

针对独立复审残留的 1 项 P1、2 项 P2 与 2 项 P3:

- **清理只认本轮创建证明**:预检拒绝或清单未记录时不再回退到 `ps`/挂载发现,栈级返回
  `retained`(`never_created`/`creation_unknown`)且不发出任何删除命令;B 容器只有确实
  返回过容器 ID 才进入清理,预先登记的名称不再作为删除依据。
- **恢复元信息预检补齐**:`statuses` 子键必须存在(与合法 null 区分)、嵌套计数必须是非负
  整数(排除 bool)、`vector_version` 必须存在且为字符串/null,全部在建目标库前拒绝。
- **资源清单未知不再报“无资源”**:记录失败时保留现场并标 `resource_state_unknown`,
  栈级 `retained` 不会被汇总成 `nothing_to_clean`。
- **报告边界**:work-dir 子目录创建失败也写部分报告;先定稿 `report` 步骤再写 JSON/摘要,
  两个文件终态一致;失败收尾实际执行清理时,报告里的 `cleanup` 步骤不再停留在 `not_run`。
- **清理幂等**:网络/卷已被外部删除时报 `not_found` 并继续处理其余对象,不再整体抛错、
  也不会因网络缺失跳过卷删除。
- **后端镜像补 `COPY eval_data`**:否则容器内的评测入口与评测测试会缺数据集。
- **验证环境变更**:本机已无 Docker Desktop,构建/测试/演练改在 WSL 发行版内的 Docker 引擎
  执行(`wsl -d Ubuntu -e bash -lc 'cd /mnt/c/.../mimo && docker compose ...'`);验证用
  合成环境文件放在仓库外,`AI_MODE`/`EMBEDDING_MODE` 为 disabled。

独立复验(另一代理实例,不复用本轮推理)结论:P1、P2、P2 三项均**已关闭**;探针
**63 passed / 1 failed**;真实反例与补充的 mock 场景未发现删错对象;新提 3 个 P3 已按上面
「清理幂等」「cleanup 步骤状态」「`archive.bytes` 排除 bool」修复并重跑。

已实际运行(2026-10-26,WSL Ubuntu + Docker 29.1.3;合成 env,零真实模型/embedding 调用):

- **后端全量 358 passed**(349 既有 + 9 项本轮回归;205.44s;1 条既有 Starlette/AnyIO 弃用提示)。
- **复审探针 63 passed / 1 failed**:唯一失败是上一轮的
  `test_review_ops_edges.py::test_network_remove_nonzero_is_not_reported_removed`,它依赖
  本轮被删除的“无清单时按名称回退清理”旧行为;同一性质由项目回归(带本轮清单)覆盖并通过。
- **真实 Docker 反例**:复用旧 `run_id` 并预先保留牺牲容器/卷/网络后执行
  `--cleanup-on-failure` 演练,CLI 退出 2(`stage=ownership`),三个旧对象全部保留,
  `cleanup.status=retained`、`removed_count=0`;修复前同场景会删除这三者。
- **完整演练 `[fixb3final20261026]` 20/20 步骤成功**:备份 54,031 bytes、sha256 `3f82a118…`、
  Alembic `d4c5b6a7c8e9`、pgvector `0.8.7`;恢复到新库 **equal=true**;同卷重建容器 ID
  `9fb75ddf…`→`a66f33ed…` 后数据/HTTP 续写仍在;源库快照未变化;生产正例与 4 类负例通过;
  清理 `cleaned`、`removed_count=16`,演练资源无残留。
- **合成评测**:39 样本,检索 24/24、分析 32/32,退出 0,评测库按 manifest 删除;12/12 机制
  断言通过,0/24 混合降级,真实模型/embedding 调用 0 次。

未验证:真实云服务器/公网 HTTPS、真实聊天模型效果、从零构建新镜像、断电/强杀恢复;
本轮未改前端,未重跑浏览器闭环。

### 真实 embedding 联调(2026-10-27,真实服务,合成评测集)

- **配置**:OpenAI 兼容 `/embeddings` 端点,模型 `qwen3.7-text-embedding-flash`,向量 **1024 维**,
  usage 由服务端真实返回;聊天模型仍 disabled,只发送脱敏后的案例/SOP 文本与查询词,密钥仅在本机被 Git 忽略的 env 中。
- **索引**:评测库内真实构建 **19 个索引单元**、2 次 embedding 调用、**2,197 prompt tokens**,
  配置指纹 `f90a0df350aeffd7`,0 降级。
- **旧 6 查询入口**(`eval_retrieval.py --allow-real-embedding`):关键词与混合均为
  Recall@3 0.75、Recall@5 0.75、MRR 0.75、空结果正确率 1.0,没有差异。
- **版本化 24 条检索样本**(复用 `eval_data/v1` 夹具与产品检索路径,含权限/版本边界):
  关键词 Recall@3 **0.9667**、Recall@5 1.0、MRR 0.9222、无答案 **7/9**;
  混合 Recall@3 **0.9333**、Recall@5 1.0、MRR 0.9333、无答案 **5/9**。
- **安全核对**:混合多出的 2 条命中都是 `published` v2(公开知识),未返回草稿/待审/历史版本;
  权限与状态过滤在真实向量下仍然成立。
- **结论**:真实链路(适配→索引→查询→混合排序→返回前复核)可用;但这套合成夹具的关键词区分度很高,
  **未观察到语义检索带来的 Recall 提升**,无答案查询反而多出误报。真实业务语料上的效果仍未验证,
  不据此宣称“混合检索更好”。
- **复现**:配置 `EMBEDDING_MODE=cloud` + `EMBEDDING_BASE_URL`/`EMBEDDING_MODEL`/`EMBEDDING_API_KEY`
  (只放本机 `.env`),运行 `python backend/scripts/eval_retrieval.py --setup --allow-real-embedding`;
  24 条样本用真实适配器跑版本化检索 runner(评测脚本在仓库外,不随仓发布)。
- **限制**:单一供应商/单一模型、6+24 条查询、合成夹具与 provisional 标签、单次运行;这不是质量结论。

### 真实聊天模型评测(2026-10-27,真实服务,合成样本 21 条)

- **模型**:阿里云 MaaS（OpenAI 兼容）`deepseek-v4.1-flash`;`AI_SUPPORTS_JSON_MODE=0`,由提示词要求 JSON 并按固定 Schema 校验。
  `mimo/.env` 里原来的 DeepSeek 配置 key 已失效(401),本次改用同一账号的 MaaS 端点。
- **范围**:`eval_data/v1` 的 21 条分析质量样本（只跑主 suite;失败注入/缓存/权限子集对真实模型无意义,未跑）。检索用真实 embedding 索引（19 单元）。
- **结果**:分类 **12/18 = 66.7%**、有效建议 13/18、应弃答 **1/3**;业务失败 **2/21**（1 条 Schema 校验失败并重试 1 次仍失败、1 条空响应）;
  4 条判 incomplete（模型把证据写成 `description`/`error_logs` 等字段名而非证据 ID,后端按规则移除并标注）;
  知识引用初次/采用/最终均 **133/133 = 100% 有效**。
- **调用与耗时**:22 次调用、20 条完整 usage、26,143 prompt + 37,951 completion tokens;进程内 API 均值 14.9s、P50 11.5s、P95 31.1s。
- **解读**:证据 ID 误用说明后端的引用校验确实在兜底;弃答准确率低说明该模型倾向「给个分类」。样本是合成夹具、标签是 provisional,不是真人 gold,
  **66.7% 不能当作真实业务准确率**,也不用于模型排名。
- **限制**:单模型、单次运行、21 条样本;未跑真实人工复核;未计算费用。

> 凭据提醒:排障时终端输出曾打印过 key,建议轮换;评测进程只放开自身限流,产品默认 2 次/分钟不变。



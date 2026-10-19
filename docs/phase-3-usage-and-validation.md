# 第三阶段操作与验证说明

更新日期：2026-10-19。AI 辅助 SOP 草稿、embedding 索引管理、混合检索三批机制已实现并通过独立复审；真实模型效果和第四阶段交付仍待验证。

## 从案例生成 SOP 草稿

1. 在故障中补齐人工确认根因、实际处理步骤和恢复验证，生成案例并提交审核。
2. 打开已审核通过的案例。运维人员仅能从自己的案例生成，管理员可按权限操作；审核人员不能生成。
3. 选择“模板生成草稿”或“AI 生成草稿”。模板仅复制字段，不调用模型；AI 根据指定案例的可信快照生成结构化内容。
4. 成功后进入新的 SOP 草稿，人工检查来源标注、待补信息、适用范围及回滚方案，再沿现有编辑、提交、审核与发布流程处理。

AI 不覆盖已有 SOP，不修改案例人工结论，不自动提交或发布，也不执行生成的命令。来源在调用前和写入草稿前均校验；变化或失效时拒绝写入。失败不留下空白或半成品 SOP。

`AI_MODE=disabled` 时使用模板或手工编辑；`demo` 显示模拟生成且用量未知；`cloud` / `local` 使用配置服务，失败不切换为演示答案。云模式会发送脱敏后的来源材料，密钥仅保存在后端。JSON 语法与结构错误共享一次有限修复预算，缺失 usage 显示未知。

同一次生成使用同一 `request_token` 去重；面板重试沿用该 token。主动再次生成使用新 token，可创建另一份草稿。接口为：

| 接口 | 用途 |
|---|---|
| `POST /api/cases/{id}/generate-sop` | 模板生成 |
| `POST /api/cases/{id}/generate-sop-ai` | AI 生成，业务结果查看生成记录状态 |
| `GET /api/cases/{id}/sop-generations` | 独立生成历史、失败信息和实际用量 |

## embedding 配置与索引管理

embedding 与聊天模型独立配置，默认关闭。支持 OpenAI 兼容的 `/embeddings` 接口：

```dotenv
EMBEDDING_MODE=disabled
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_MODEL=
EMBEDDING_TIMEOUT_SECONDS=30
EMBEDDING_BATCH_SIZE=16
EMBEDDING_MAX_INPUT_CHARS=6000
EMBEDDING_RATE_LIMIT_PER_MINUTE=30
EMBEDDING_MAX_CONCURRENT_PER_USER=4
```

使用真实服务时将模式设为 `cloud` 或 `local`，填写服务前缀、模型和可选密钥，再执行 `docker compose up -d backend` 载入配置。`LOCAL_ONLY=1` 禁止聊天或 embedding 配为 cloud；local 仅允许受信任本机/内网目标，不继承环境 HTTP 代理，失败不切服务。

数据库使用 `pgvector/pgvector:pg16`，迁移创建 `vector` 扩展。既有卷须保留，缺少扩展或权限时应按错误排查。索引只纳入已通过案例与当前有效已发布 SOP，排除待审、草稿、历史和废弃内容。启动、登录与页面加载不会自动构建。

以下命令在仓库根目录执行，兼容 PowerShell 7 和 Bash。数据库须已启动、迁移完成；状态查询不调用模型：

```powershell
docker compose run --rm backend python scripts/build_embedding_index.py --status
```

以下操作会调用配置的 embedding 服务。cloud 模式会发送脱敏后的知识文本，应先确认目标配置及可发送的知识范围：

```powershell
# 构建/刷新：同指纹、同内容可复用向量
docker compose run --rm backend python scripts/build_embedding_index.py

# 强制重建：使用新代次
docker compose run --rm backend python scripts/build_embedding_index.py --rebuild
```

同等管理 API 仅对管理员开放：`GET /api/admin/embedding-index`、`POST /api/admin/embedding-index/build`、`POST /api/admin/embedding-index/rebuild`。命令行由具备部署环境访问权的维护人员执行，不经过浏览器角色登录。

配置指纹绑定模式、端点、模型和文本版本，不含密钥；模型、端点或文本版本变化须重建，同维度不同模型不混用。结果检查 `built_units`、`reused_units`、`embedded_calls`、`skipped` 和状态，`partial` 不表示完整构建。失败不激活半成品；兼容的完整旧代次仍可用，不兼容时退回关键词。并发构建使用数据库领取机制，中断或失效执行者不能提交当前代次。

上游 429 标记 `rate_limited` 且不重试：索引构建失败并记录实际调用数；查询向量生成直接降级关键词。超时、连接错误、5xx 仅在退避 0.5 秒后重试一次，仍失败即结束。

## 混合检索与助手

`GET /api/search?q=...` 与右侧助手共用检索路径。兼容活动索引可用时，数据库按向量距离取候选，再用 RRF 与关键词结果融合、去重和稳定排序；不展示未经校准的置信百分比。查询 embedding 使用独立限流和并发额度，不挤占聊天额度。

状态、权限和有效版本在候选排名前过滤；内容指纹识别陈旧向量。全部返回出口再次校验来源，助手发给聊天模型前再次复核当前片段并保留查询相关依据，模型返回后仍校验引用。缓存也绑定检索策略、索引代次和配置，命中时不跳过来源校验。

响应字段 `retrieval_mode`、`degraded`、`degrade_reason` 标识实际路径。embedding 关闭、索引不兼容或服务失败时，页面显示关键词方式及适用的降级原因；完全失败不能显示为“无匹配”。`keyword_only=true` 明确禁止查询向量调用；前端 AI 状态未知时只走该路径，并提示查询文本未发送。cloud 状态确认后，页面说明查询和知识片段的外发范围。

## 检索评测

`backend/scripts/eval_retrieval.py` 在独立数据库 `opssop_eval` 中写入合成夹具并强制构建对应来源的索引，不使用业务库作为评测库。仍应在独立测试环境执行；数据库账号需要创建数据库与扩展的权限。

以下 PowerShell 7 命令使用脚本内合成向量，仅验证排序机制、失败处理与统计口径，不调用真实 embedding 服务：

```powershell
docker compose run --rm -e EMBEDDING_MODE=cloud -e EMBEDDING_BASE_URL=https://synthetic.invalid/v1 -e EMBEDDING_MODEL=synthetic-eval -e LOCAL_ONLY=0 backend python scripts/eval_retrieval.py --setup --synthetic
```

已有夹具时可以去掉 `--setup` 复跑。仅当目标真实服务和材料范围已确认时，才执行真实评测；此命令会调用当前配置的 embedding 服务并重建评测索引：

```powershell
docker compose run --rm backend python scripts/eval_retrieval.py --setup
```

评测查询覆盖问法改写、精确错误码、相同现象不同根因、无答案及来源失效。报告包含 Recall@3/5、MRR、空结果正确率；空答案查询出现任何命中均计误报。退出码 0 表示所有查询均走 hybrid 且指标可用，2 表示配置/夹具/索引构建错误，3 表示存在降级，混合指标标为 `unverified` 并报告降级率。合成指标不能作为真实语义效果或质量提升结论。

`--cleanup` 仅删除该评测库中脚本识别的评测夹具，属于数据清理操作；需要时先检查目标环境再执行。

## 已验证与待交付

2026-10-19 独立复审使用隔离临时数据库和合成响应，后端 **240 项完整回归通过**，另有 **19 项独立探针通过**。前端无变化，沿用此前独立通过的 **34 项测试、类型检查和生产构建**；本轮未重复执行。完整浏览器脚本仅修正按钮名并检查语法，本轮未重新运行。

真实 embedding 语义效果、第三阶段 AI SOP 生成质量、local 模型、其他云服务、Linux 云部署和备份恢复均未验证。第二阶段历史 DeepSeek 分析验证不代表第三阶段真实生成或检索验收。

第四阶段待完成：助手案例来源定位入口、统一 AI 完整闭环、完整效果评测、演示种子场景、Linux 部署与备份恢复演练。三批机制复审通过可作为进入该阶段的依据，不能写成项目已全部交付。

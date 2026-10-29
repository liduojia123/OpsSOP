# OpsSOP AI 配置指南

适用版本：2.1.0。

聊天模型负责故障分析和 SOP 草稿整理；embedding 模型负责知识向量化与混合检索。两者独立配置，均为可选能力。关闭模型后，业务流程和关键词检索仍可使用。

## 配置方式

以根目录的 [.env.example](../.env.example) 为参考，将实际配置写入被 Git 忽略的环境文件。密钥只由后端读取，不填写到前端代码。修改环境文件后重新创建后端容器以应用配置：

```bash
docker compose up -d --force-recreate backend
```

使用独立项目名或环境文件部署时，沿用原有的 `-p` 与 `--env-file` 参数。生产部署参见 [部署指南](deployment.md)。

## 聊天模型

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AI_MODE` | `disabled` | `disabled`、`demo`、`cloud` 或 `local` |
| `AI_BASE_URL` | 空 | OpenAI 兼容 API 基础地址，后端追加 `/chat/completions` |
| `AI_MODEL` | 空 | 目标服务支持的模型标识 |
| `AI_API_KEY` | 空 | 可选 Bearer 密钥，是否必填由服务决定 |
| `AI_TIMEOUT_SECONDS` | `60` | 单次请求超时秒数 |
| `AI_SUPPORTS_JSON_MODE` | `0` | 服务支持时开启 `response_format=json_object` |
| `AI_SUPPORTS_USAGE` | `0` | 显式声明目标服务的 usage 能力 |
| `AI_MAX_CONTEXT_CHARS` | `24000` | 脱敏后上下文字符上限 |
| `AI_MAX_OUTPUT_TOKENS` | `2048` | 输出 token 上限 |
| `AI_RATE_LIMIT_PER_MINUTE` | `6` | 单用户每分钟分析限额 |
| `AI_MAX_CONCURRENT_PER_USER` | `2` | 单用户并发限制 |

`disabled` 不调用模型；`demo` 返回明确标注的模拟结果；`cloud` 调用配置的外部接口；`local` 调用受信任的本机或内网接口。各模式不在失败后自动切换。

云端配置示例，需替换地址、模型名和密钥：

```dotenv
AI_MODE=cloud
AI_BASE_URL=https://model.example.com/v1
AI_MODEL=your-chat-model
AI_API_KEY=your-private-key
AI_SUPPORTS_JSON_MODE=0
AI_SUPPORTS_USAGE=0
LOCAL_ONLY=0
```

基础地址应包含服务要求的版本路径，不填写完整 `/chat/completions` 地址。结构化输出与参数兼容性以目标服务为准；不支持 JSON 模式时保持关闭，系统仍会按固定 Schema 校验输出。语法或结构错误共享有限修复预算；失败保留错误记录，不伪装为成功。

usage 仅记录接口实际返回的字段，缺失时显示未知，不据此编造 token 数或费用。限流状态为单后端实例的内存计数，多实例部署需在外部网关补充统一限制。

## embedding 模型

| 变量 | 默认值 | 用途 |
|---|---|---|
| `EMBEDDING_MODE` | `disabled` | `disabled`、`cloud` 或 `local` |
| `EMBEDDING_BASE_URL` | 空 | OpenAI 兼容基础地址，后端追加 `/embeddings` |
| `EMBEDDING_MODEL` | 空 | embedding 模型标识 |
| `EMBEDDING_API_KEY` | 空 | 可选 Bearer 密钥 |
| `EMBEDDING_TIMEOUT_SECONDS` | `30` | 单次请求超时秒数 |
| `EMBEDDING_BATCH_SIZE` | `16` | 每批文本上限 |
| `EMBEDDING_MAX_INPUT_CHARS` | `6000` | 每条文本脱敏后的字符上限 |
| `EMBEDDING_RATE_LIMIT_PER_MINUTE` | `30` | 单用户查询向量限额 |
| `EMBEDDING_MAX_CONCURRENT_PER_USER` | `4` | 单用户查询向量并发限制 |

```dotenv
EMBEDDING_MODE=cloud
EMBEDDING_BASE_URL=https://embedding.example.com/v1
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_API_KEY=your-private-key
LOCAL_ONLY=0
```

聊天与 embedding 可以使用不同服务、模型和密钥。向量维度从实际响应校验，条数、索引位置、维度不一致、空向量及非有限数值均会拒绝。超时、连接和 5xx 错误有一次有限重试，429 不自动重试；查询失败降级关键词，索引构建失败显示原因。

## 本机、内网与 local-only

本地配置使用 `AI_MODE=local` 和/或 `EMBEDDING_MODE=local`，配置实际可达的 OpenAI 兼容地址。后端容器内的 `localhost` 指向容器本身；在 Docker Desktop 访问宿主模型时使用 `host.docker.internal`，其他部署按实际内网地址配置。

local 模式拒绝明显的公网目标，允许受信任宿主名、内网/环回 IPv4 及指定内网域名后缀；HTTP 客户端不继承环境代理。它不会自动下载模型、开放服务端口或创建公网隧道。

```dotenv
AI_MODE=local
AI_BASE_URL=http://host.docker.internal:11434/v1
AI_MODEL=your-local-chat-model
AI_API_KEY=
EMBEDDING_MODE=disabled
LOCAL_ONLY=1
```

`LOCAL_ONLY=1` 会同时拒绝聊天和 embedding 的 `cloud` 配置，启动及调用时均执行约束，不在故障后切云。该开关属于应用约束；需要严格禁止出口时，还应由部署环境限制网络出口并确保内网域名解析受控。

## 知识索引

只有已通过案例与当前有效已发布 SOP 纳入向量索引。故障原始历史、未审核案例、草稿、历史和废弃 SOP 不参与正式索引。文本在发送前脱敏并限制长度，向量存储在部署数据库的 pgvector 中。

状态查询不触发模型请求，构建与重建由管理员主动执行：

```bash
# 查看状态
docker compose run --rm backend python scripts/build_embedding_index.py --status

# 构建或刷新；未变化内容可复用向量
docker compose run --rm backend python scripts/build_embedding_index.py

# 强制重新生成索引代次
docker compose run --rm backend python scripts/build_embedding_index.py --rebuild
```

管理员 API：`GET /api/admin/embedding-index`、`POST /api/admin/embedding-index/build`、`POST /api/admin/embedding-index/rebuild`。普通运维与审核账号不能调用。

状态包括未配置、待构建、构建中、可用、需要重建和失败。端点、模型或索引文本版本变化后需要重建；维度相同也不能混用不同模型。配置指纹不含密钥。

构建以独立代次生成，完整可用后再切换活动代次。失败不激活半成品；与当前配置兼容的旧代次可继续使用，配置不兼容则回退关键词。构建期间来源变化会跳过对应条目并明确报告，重新构建可补齐。

## 检索与数据发送范围

关键词检索无需模型。混合检索使用关键词排名和向量排名融合，在排序和返回前执行权限、审核状态、SOP 有效版本及来源内容复核。索引未就绪、配置变化、限流或服务错误会标明降级原因；不会将失败写成知识库无答案。

云端分析会发送脱敏后的问题、日志及已授权知识片段；AI 生成 SOP 只发送指定已通过案例及必要证据；云端 embedding 会发送脱敏后的索引文本和查询文本。脱敏规则不能覆盖全部敏感信息，发送范围应符合部署组织的数据要求。

查看模型状态失败时，前端只使用明确的关键词路径。API 使用者可通过 `GET /api/search?keyword_only=true` 强制零 embedding 调用的关键词检索。

运行评测和理解指标参见 [评测指南](evaluation.md)；功能操作参见 [产品使用指南](product-guide.md)。

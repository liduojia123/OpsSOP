# OpsSOP 评测指南

适用版本：3.6.0。下列既有评测保留各自执行日期与环境，不代表在 2.0.0 重新执行；前端交付记录见[界面与动效](ui-redesign.md)。

评测工具用于检查检索、AI 分析及异常处理机制，并输出可追踪的指标。合成适配器检查系统行为，真实 API 运行观察指定模型在指定数据集上的表现，两类结果分别解释。

## 自动化检查入口

后端检查使用独立 PostgreSQL 测试库，`POSTGRES_TEST_DB` 必须与 `POSTGRES_DB` 不同。先在隔离的 `.env.eval` 中配置数据库、独立端口和基础口令，使用非生产环境；以下命令供维护者按变更需要执行：

```text
docker compose -p opssop-eval --env-file .env.eval up -d db
docker compose -p opssop-eval --env-file .env.eval build backend
docker compose -p opssop-eval --env-file .env.eval run --rm --no-deps backend python -m pytest tests/ -q
```

前端在 `frontend/` 目录执行 `npm ci`、`npm test` 和 `npm run build`。PowerShell 使用 `npm.cmd`。业务闭环工具位于 `scripts/e2e_loop.py`、`scripts/browser_e2e.py` 与 `scripts/demo_browser_e2e.py`，仅指向独立模拟环境，账号和目标从环境变量读取，不对生产库运行。

## 版本化数据集

内置数据位于 [`backend/eval_data/v1/`](../backend/eval_data/v1/)，版本为 `2026-10-22.1`：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 版本、标签来源、人工复核标识和数据数量 |
| `knowledge.json` | 25 条合成案例/SOP，含审核、权限和版本边界 |
| `samples.json` | 39 条样本；用于 24 条检索查询、21 条主分析输入及异常/缓存/权限检查 |
| `labels.json` | 合成参考标签，状态 `provisional`，未经真人复核 |
| `synthetic_behaviours.json` | 超时、429、JSON/Schema 错误、引用失效和部分 usage 等受控响应 |

检索与分析可复用样本，因此各套件请求数不能相加当作独立样本数。来源通过稳定键映射到本轮数据库 ID 与 SOP 版本，不依赖标题或固定自增 ID。

数据覆盖明确分类、问法改写、同表象不同根因、信息不足、无答案、注入文本、权限和版本边界。内置标签是开发 AI 编写的参考值，不作为真人审核后的业务基准。

## 运行合成评测

先在独立环境文件 `.env.eval` 配置数据库和独立端口，使用 `OPSSOP_ENV=development`、`AI_MODE=disabled`、`EMBEDDING_MODE=disabled`、`LOCAL_ONLY=1`，然后启动专用数据库。评测入口自行注入合成适配器，不需要真实 API Key。

以下命令从仓库根目录执行，单行写法适用于 PowerShell 7 或 Linux shell：

```bash
docker compose -p opssop-eval --env-file .env.eval up -d db
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_suite.py --suite all --mode synthetic
```

仅运行某一套件或限制输入数量：

```bash
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_suite.py --suite retrieval --mode synthetic
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_suite.py --suite analysis --mode synthetic --limit 5
```

使用 `--dataset` 指定兼容格式的数据目录，`--run-id` 指定运行标识，`--keep-db` 保留本轮库，`--reuse` 仅复用 manifest 匹配的库。`--limit` 输出部分结果，不能把未执行项目记为通过。

每轮创建 `opssop_eval_<run_id>` 专用数据库，执行正式 Alembic 迁移，并记录运行标识与数据集哈希。目标库不得与业务库、测试库或系统库重名，production 环境拒绝评测。完整套件默认结束后按 manifest 清理专用库。

需要手动清理保留库时，使用该轮输出的准确值替换下列占位符：

```bash
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_suite.py --cleanup --target-db opssop_eval_RUN_ID --run-id RUN_ID
```

## 报告与退出码

默认输出 `scripts/eval-artifacts/<run_id>/report.json` 与 `summary.md`，包含代码指纹、数据哈希、分组结果、已执行/未执行数量、指标分母、失败和引用观察记录。目录相对于后端脚本的仓库根路径计算。

容器内默认输出不自动保留到宿主机。需要保存时，为运行容器挂载宿主输出目录，并将 `--out` 指向挂载点；或保留具名容器，退出后使用 `docker cp` 导出报告，再移除容器。不要在带 `--rm` 的临时容器退出后尝试复制。

| 退出码 | 含义 |
|---|---|
| `0` | 执行完成，机制断言通过 |
| `2` | 配置、数据集或数据库准备错误 |
| `3` | 混合检索降级，整批混合指标不可用 |
| `4` | 机制断言失败、运行中断或清理失败 |
| `130` | 用户取消，保留已完成的部分结果后退出 |

退出 0 表示工具与断言符合要求，不代表真实模型质量达到业务目标。取消、报告不完整和清理失败均需结合报告状态判断。

## 指标口径

所有比例包含 `numerator`、`denominator`、`value`、`status` 与定义。分母为零时使用 `null / not_applicable`，不填 0% 或 100%。

| 指标 | 定义 |
|---|---|
| Recall@3 / Recall@5 | 对有参考答案的查询，计算前 3/5 条命中的不同目标来源比例，再按查询宏平均；失败查询计 0 |
| MRR | 第一个正确来源排名倒数的宏平均 |
| 无答案正确率 | 参考答案为空时，仅 HTTP 成功且返回零条正式来源算正确；任何命中算误报 |
| 分类正确率 | 正确有效的分类建议数 / 全部计划非弃答样本；失败、无建议和非法分类仍在分母 |
| 应弃答正确率 | 应弃答样本中成功处理且不给分类或标明证据不足的比例 |
| 业务失败率 | HTTP 错误及业务 `failed` 请求 / 全部计划请求，同一请求不重复计数 |
| 引用有效率 | 分别观察清洗前初次输出、最终采用输出及后端处理后引用，校验来源、版本与下发范围 |
| 耗时 | 进程内 API 的均值、P50、P95，使用单调时钟与最近秩百分位 |

来源按 `(source_type, source_id, version)` 去重，SOP 必须匹配有效版本。任一正常混合查询降级或失败时，整批混合指标标为 `unverified`，不只发布成功子集；关键词指标仍可读取。

无法解析的模型输出属于不可观察样本，不能记为引用全部有效。引用 ID 有效不等于引用内容足以支持结论。`incomplete`、失败、缓存、权限和异常注入分别统计。无真人复核记录时人工修改比例为 `not_available`；usage 缺失保持未知，不推算费用。进程内耗时不等于生产网络端到端延迟。

## 真实 API 评测入口

`eval_suite.py` 的完整 CLI 当前仅执行 `synthetic`，`--mode real` 即使携带 `--allow-real` 也会拒绝执行。扩展真实模式应复用版本化 runner，并记录模型配置、标签来源与调用边界。

独立检索入口支持六条查询的真实 embedding 对比。先按 [AI 配置指南](ai-configuration.md) 配置目标服务，再显式允许调用：

```bash
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_retrieval.py --setup --allow-real-embedding
```

该入口会构建真实索引，并保留专用评测库；执行后使用输出中的 `target_db` 和 `run_id` 精确清理：

```bash
docker compose -p opssop-eval --env-file .env.eval run --rm backend python scripts/eval_retrieval.py --cleanup --target-db opssop_eval_RUN_ID --run-id RUN_ID
```

真实运行会向配置服务发送脱敏后的合成知识与查询，可能产生服务用量。无真实服务时使用合成完整套件，不以合成向量成绩宣称真实语义效果。

## 参考结果：2026-10-27

以下结果来自真实 API 和内置合成数据，使用专用 runner 执行；该 runner 不作为随仓 CLI 交付。样本标签未经真人审核，单次、单模型结果不代表真实业务准确率或产品服务承诺。

| 项目 | 样本与模型 | 观察结果 |
|---|---|---|
| 分析质量 | 21 条输入；阿里云 MaaS `deepseek-v4.1-flash` | 非弃答分类 12/18；应弃答 1/3；业务失败 2/21；4 条 incomplete |
| 知识引用 | 上述分析中可观察的 19 条报告 | 初次、采用和最终知识引用均 133/133 有效；2 条失败输出不可观察 |
| 检索 | 24 条查询；`qwen3.7-text-embedding-flash`、1024 维 | Recall@3：关键词 0.9667，混合 0.9333；无答案正确数：7/9 与 5/9 |

该数据集未显示混合检索整体优于关键词；两路 Recall@5 均为 1.0。需要使用有权处理的真实业务语料和真人标注，再决定模型、阈值及上线验收指标。

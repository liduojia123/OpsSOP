# OpsSOP 运维操作索引

本目录覆盖第四阶段第三批交付的备份、恢复、隔离演练、命名卷持久化和生产部署操作。
所有命令都以**明确的项目/运行 ID/目标库**为前提,不使用模糊名称清理资源。

- [备份与恢复](backup-restore.md)
- [生产部署与本地 Linux 容器 smoke](deployment.md)

## 1. 资源模型

| 资源 | 命名/归属 | 说明 |
|---|---|---|
| Compose 项目 | `opssop-<用途>-<run_id>` | 脚本拒绝非 `opssop-` 前缀项目,避免误触既有 `mimo` 项目 |
| 命名卷 | Compose 项目名 + `_pgdata` | 演练/生产各自新建;不使用 `mimo_pgdata` |
| 运行 ID | 6–41 位小写字母/数字/`_`/`-` | 写入容器/卷/网络标签 `com.opssop.run_id` |
| 恢复目标 | `opssop_restore_<run_id>` 等新库 | 已存在、来源库、系统/模板/测试/演示库一律拒绝 |
| 演练产物 | 仓库外 `work-dir` | JSON/摘要/备份归档;归档含业务数据,按私有数据保存 |

清理前必须同时核对:确切容器/卷/网络名称、`com.docker.compose.project`、`com.opssop.run_id` 和本轮创建记录。
不执行通用 `docker compose down -v`、`docker system prune` 或删除既有卷。

## 2. 隔离演练

```powershell
# PowerShell 7:宿主机执行;需要 Docker CLI 与 Python 3.10+
$run = "drill20261022a"
$work = "D:\opssop-drill-$run"
python backend/scripts/ops_cli.py drill `
  --work-dir $work `
  --run-id $run `
  --timeout 180
```

```bash
# Linux:与上面等价
RUN_ID=drill20261022a
python3 backend/scripts/ops_cli.py drill \
  --work-dir "/var/tmp/opssop-drill-$RUN_ID" \
  --run-id "$RUN_ID" --timeout 180
```

- 使用已有依赖镜像 `opssop-review-backend:20261019` + 当前 `backend/` 源码只读挂载,
  加上本地 `nginx:1.27-alpine` 和 `pgvector/pgvector:pg16`;**不是新镜像构建成功**,也不是云服务器验收。
- 演练 Compose 子进程使用受控环境:剔除宿主同名插值变量和 `COMPOSE_*` 覆盖项,
  启动前核对最终数据库、挂载、`AI_MODE`/`EMBEDDING_MODE=disabled`、端点/密钥为空、网络和端口。
- 首次 `up` 前硬校验本轮 Compose 项目/容器/卷/网络均为全新;已有同名对象(包括标签不同的
  旧卷)在启动和迁移前直接拒绝,不会先写库再报告不匹配。
- 默认成功时只按本轮创建清单和确切名称/ID/标签清理;失败默认保留现场诊断,
  可用 `--cleanup-on-failure` 显式改变。清理结果区分 `cleaned`/`retained`/`not_found`/
  `nothing_to_clean`/`failed`,任何拒绝或命令非零都会使整轮非成功。
- 传 `--keep-resources` 时保留资源,结果 JSON 会列出名称和保留状态。
- 输出:`<work-dir>/result.json` 与 `<work-dir>/summary.md`;准备阶段(环境文件、镜像、
  Compose 配置)失败也会写非成功部分报告,当前步骤标记 failed,未执行步骤标记 not_run 并说明原因。
  备份归档在 `<work-dir>/backup/`,含账号哈希与业务内容,按私有数据保存,不提交公开仓库。

### 演练覆盖

1. 新建项目/命名卷/源库 A,正式 `alembic upgrade head`。
2. 通过真实 HTTP API 创建合成账号、故障/补证、人工确认案例、SOP v1/v2 版本链、草稿、
   待审案例、无权对照、反馈;只有初始管理员/分类由启动 seed 创建。
3. 权限/检索 HTTP 检查后才采集备份时点快照,`pg_dump -Fc` + SHA-256 + 元信息。
4. 恢复到全新库 B;核对计数/状态/业务摘要/Alembic 版本/序列续分配。
5. 启动指向 B 的后端与 nginx,真实 HTTP 登录、检索、权限、静态首页/SPA 路由,
   继续新建故障/补证/确认/审核,核对审计;证明 A 未被写入。
6. 数据库正常重启后数据仍在;删除 DB 容器,用同一命名卷重建(容器 ID 变化),
   再次 HTTP 登录/查询/续写并核对审计。
7. 生产合并配置检查、生产正例 smoke、弱口令/演示开关/遗留演示账号/迁移失败负例。
8. 按标签精确清理,并在结果 JSON 记录每个资源的清理/保留状态。

## 3. 已验证边界

- 数据库/后端不发布宿主端口;演练/生产前端只绑定 `127.0.0.1`;nginx `/api/` 同源代理可用。
- 已有目标库(包括空库)拒绝恢复;SHA/大小不符、归档不可读在创建目标前拒绝。
- `pg_restore` 失败/取消非零;默认保留失败目标,显式 `--cleanup-failed-target` 才清理本轮创建的目标。
- 生产弱口令/演示开关在 Alembic 迁移前失败;遗留演示账号在 seed 检查处拒绝;迁移失败不启动 API。
- 清理使用确切名称/标签,不删除 `mimo_pgdata` 或既有卷。

## 4. 仍未验证

- 目标云服务器、公网 HTTPS/证书链、真实 DNS/防火墙;
- 真实聊天模型质量、真实 embedding、真人标签复核;
- 强制终止进程/断电后的自动恢复、自动异地容灾、自动调度/保留策略;
- 演练网络未启用 Docker `internal` 出口隔离(在 Docker Desktop 上会阻断宿主访问已发布端口,
  而演练需要从宿主做真实 HTTP 检查);本轮通过受控子进程环境、禁用的模型配置和空端点
  约束外发,不把它声称为网络层隔离;
- 新镜像从零构建(Docker Hub/依赖网络可用性)未作为本批证据。

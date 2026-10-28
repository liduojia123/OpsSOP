# OpsSOP 2.0.0 运维指南

生产部署使用 Compose 项目 `opssop-prod`。日常操作沿用安装时的环境文件、项目名和数据卷；备份与演练产物保存在仓库外。

- [部署与升级](deployment.md)
- [备份与恢复](backup-restore.md)

## 日常操作

以下命令从发行源码根目录执行，使用 PowerShell 7 续行语法：

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml ps

docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml logs --tail 100 backend

docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml restart backend frontend
```

查看日志时避免公开业务文本或私有配置。重启后检查登录、故障保存与正式知识检索。模型接口不可用时，人工业务与关键词检索仍可继续；索引需要重建时，由管理员主动执行构建，不在启动或登录时自动发起。

按实际恢复目标制定备份频率、维护窗口、离机副本及保留期限。升级前先备份，定期执行全新库恢复与业务续办。命名卷用于持久化，不能替代独立备份。

## 资源与归属

| 资源 | 命名或位置 | 用途 |
|---|---|---|
| 生产 Compose 项目 | `opssop-prod` | 正式业务实例 |
| 生产命名卷 | 默认 `opssop-prod_pgdata` | PostgreSQL 数据 |
| 演练源项目 | `opssop-drill-<run_id>` | 独立合成业务、备份与恢复 |
| 演练生产配置项目 | `opssop-prod-<run_id>` | 独立检查生产配置及启动行为 |
| 演练资源标签 | `com.opssop.purpose=drill`、`com.opssop.run_id` | 与 Compose 项目标签共同标识归属 |
| 恢复目标库 | `opssop_restore_<run_id>` | 显式创建的新库，不覆盖来源库 |
| 演练工作目录 | 仓库外独立目录 | 报告、临时环境配置及备份归档 |

`run_id` 为 6–41 位小写字母、数字、下划线或连字符，以字母或数字开头。为兼容演练自动生成的数据库名，建议使用不含连字符、长度不超过 20 字符的 ID；每次使用新 ID 和新工作目录。

资源清理同时校验确切名称或 ID、Compose 项目标签、运行 ID 和本次创建记录。归属或创建记录不明时保留现场。维护流程不使用 `docker compose down -v`、`docker system prune` 或通配删除卷。

## 准备隔离演练

演练在 Docker 引擎所在环境执行，需要 Python 3.10+、Docker CLI、Node.js 与 npm。Python 工具只依赖标准库。演练使用当前发行源码、构建后的前端文件以及本地后端镜像，创建独立数据库和资源，不使用生产业务库。

准备后端镜像和基础镜像：

```powershell
docker build --tag opssop-backend:2.0.0 ./backend
docker pull pgvector/pgvector:pg16
docker pull nginx:1.27-alpine

Push-Location frontend
npm.cmd ci --no-audit --no-fund
Pop-Location
```

Linux 使用 `npm` 代替 `npm.cmd`，在 `frontend` 目录执行相同依赖安装。构建和拉取需要相应镜像仓库及依赖源可达；隔离环境应预先准备镜像和依赖。

## 执行演练

PowerShell 7：

```powershell
$run = 'drill' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$work = Join-Path $env:TEMP "opssop-drill-$run"
python backend/scripts/ops_cli.py drill `
  --work-dir $work `
  --run-id $run `
  --backend-image opssop-backend:2.0.0 `
  --timeout 180
```

Linux：

```bash
RUN_ID="drill$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
python3 backend/scripts/ops_cli.py drill \
  --work-dir "/var/tmp/opssop-drill-$RUN_ID" \
  --run-id "$RUN_ID" \
  --backend-image opssop-backend:2.0.0 \
  --timeout 180
```

程序默认执行前端构建。已准备当前发行版本 `frontend/dist` 时，可用 `--skip-frontend-build` 跳过构建；不要将旧版本静态文件与新版本后端混用。

演练子进程使用生成的环境配置，清除冲突的 Compose 插值变量，并在启动前检查数据库、挂载、网络和端口。聊天与 embedding 均关闭，模型端点和密钥为空。程序会拒绝已有同名项目、容器、卷或网络，避免复用旧运行资源。

### 演练流程

1. 创建独立 Compose 项目、命名卷和源库，执行正式 Alembic 迁移。
2. 通过 HTTP API 创建合成账号、故障、证据记录、案例及 SOP 版本链，包括未审核和无权访问的对照数据。
3. 检查权限与正式知识检索，采集备份快照，生成 `pg_dump -Fc` 归档、SHA-256 与元信息。
4. 恢复到全新目标库，比对表计数、状态分布、业务摘要、Alembic 和 pgvector 版本。
5. 启动指向恢复库的应用，通过 HTTP 登录、检索、权限检查和业务续写，核对审计及序列续分配，同时检查来源库未变化。
6. 正常重启数据库，并在保留同一命名卷的情况下重建数据库容器，再检查数据与业务续写。
7. 检查生产覆盖配置、正常启动，以及弱口令、演示开关、遗留不安全账号和迁移失败时的拒绝行为。
8. 按创建记录与标签清理资源，输出报告。

### 报告与收尾

- `<work-dir>/result.json`：每个步骤、资源、备份恢复结果及清理状态。
- `<work-dir>/summary.md`：可读摘要。
- `<work-dir>/backup/`：数据库归档及配套元信息。

成功运行默认清理本次资源；失败默认保留现场。`--keep-resources` 保留运行资源，`--cleanup-on-failure` 允许失败时按本次创建记录清理。报告区分 `cleaned`、`retained`、`not_found`、`nothing_to_clean` 和 `failed`；有清理失败时不判整轮成功。

| 退出码 | 含义 |
|---|---|
| `0` | 操作成功，所需收尾完成 |
| `2` | 参数或前置检查拒绝 |
| `4` | 执行、核对、报告或清理失败 |
| `130` | 可捕获的用户取消 |

失败后先查看报告中的步骤和资源清单，不通过删除已有资源强行复用同一运行 ID。工作目录中的环境文件、账号信息与归档属于私有运维资料，应限制访问；完成处置后按保留策略保存或清理。

## 适用边界

演练覆盖数据库正常重启与容器重建，不替代断电、强杀、多机容灾和目标服务器故障演练。演练网络未启用 Docker `internal` 出口隔离；关闭模型配置属于应用约束，严格隔离仍须在宿主或网络层限制出口。

TLS 证书、DNS、防火墙和外部模型服务按实际部署环境管理。应用不提供自动异地容灾、定时备份或保留策略执行器，可由现有运维平台调度本文命令并保留退出码与报告。

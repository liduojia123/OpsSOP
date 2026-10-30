# OpsSOP 3.5.1 备份与恢复

运维入口为 `backend/scripts/ops_cli.py`，在 Docker 引擎所在宿主环境执行，只依赖 Python 3.10+ 标准库及 Docker CLI。本文生产示例使用项目 `opssop-prod`、`.env.production` 和生产 Compose 覆盖；操作已有安装时使用其实际项目名与配置。

归档通过容器内 PostgreSQL 工具及二进制文件复制传递，不通过 PowerShell 文本管道处理。备份目录、元信息和恢复报告保存在仓库外。

## 平台内置备份（日常）

生产模式由平台自己执行每日备份，不依赖宿主 cron：容器内 `pg_dump` 写入 `BACKUP_DIR`，默认 `/data/OpsSop/BackUp`，按 `BACKUP_HOUR`（默认 02:00，容器 `TZ`）执行，保留 `BACKUP_RETENTION_DAYS`（默认 30）天。管理员在「系统管理 → 数据备份」可以查看、手动触发和下载；每次手动触发/下载都会写审计记录。

- 目录由 Compose 挂载到宿主机 `BACKUP_HOST_DIR`；目录与文件权限收窄，归档包含账号哈希，必须限制访问。
- 平台备份失败会写 `backup.failed` 审计并保持服务继续运行；管理员可在页面手动重试。配置 `EMAIL_MODE=smtp` 与 `ALERT_EMAIL_TO` 后，失败或超过 26 小时无成功备份会向管理员发邮件告警。
- 首次启动若已过当天执行时间且当天没有备份，会补跑一次；单实例下使用进程锁避免并发。
- 平台备份是**同机备份**，不能替代异机归档；用下面的 `offsite` 子命令补齐（见「异地副本」）。

## 异地副本

平台备份与数据库同机同盘，整机或磁盘故障会同时丢数据与备份。`ops_cli.py offsite` 把成对的 `.dump`/`.json` 复制到另一台主机并在远端校验 SHA-256：

```bash
python3 backend/scripts/ops_cli.py offsite \
  --archive /data/OpsSop/BackUp/opssop-<时间>.dump \
  --metadata /data/OpsSop/BackUp/opssop-<时间>.json \
  --host 192.168.1.30 --user backup --remote-dir /srv/opssop-offsite \
  --identity ~/.ssh/opssop_offsite --keep 14 --out-dir /var/log/opssop-offsite
```

- 前置：目标主机可 SSH 登录，执行端有系统 `ssh`/`scp`，并使用免密公钥（`ssh-keygen` 生成、`ssh-copy-id` 安装）。工具不保存远端口令。
- 远端目录权限 `700`、文件 `600`；远端按修改时间保留最近 `--keep` 份（默认 14，`0` 表示不清理），删除时成对删除 `.dump`/`.json`。
- 成功输出 `success: true`、`sha256_verified: true`；失败返回非零，便于监控。传输经 SSH 加密；归档含账号哈希，静态加密建议使用加密盘，或先 gpg 加密再复制并单独保管私钥。
- 每日自动：在宿主 cron 的平台备份之后追加同一命令，例如：

```cron
30 2 * * * cd /opt/opssop && python3 backend/scripts/ops_cli.py offsite \
  --archive "$(ls -1t /data/OpsSop/BackUp/opssop-*.dump | head -1)" \
  --metadata "$(ls -1t /data/OpsSop/BackUp/opssop-*.json | head -1)" \
  --host 192.168.1.30 --user backup --remote-dir /srv/opssop-offsite \
  --identity /root/.ssh/opssop_offsite --keep 14 >> /var/log/opssop-offsite.log 2>&1
```

从异地副本恢复前先复制回本地，再按「恢复到全新目标库」执行；恢复演练应至少覆盖一次异地副本。

### 每日异地副本任务

`backend/scripts/offsite_daily.py` 是上面命令的定时封装：在宿主执行，自动完成「取当天最新备份 → 异地复制 → 远端校验 → 记录状态」。

```bash
python3 backend/scripts/offsite_daily.py --env-file .env.production
```

- 取件方式：平台备份在容器内（`/data/OpsSop/BackUp`），脚本用 `docker exec` / `docker cp` 取件，不依赖宿主目录权限。
- 幂等：状态文件（默认 `~/.opssop/offsite-state.json`）记录已复制的备份名，同一备份只复制一次，因此可以按小时调度而不产生重复流量。
- 过期保护：备份超过 `OFFSITE_MAX_AGE_HOURS`（默认 30 小时）不复制，避免把陈旧备份当新备份上传。
- 失败可观测：失败时按 `ALERT_EMAIL_TO` 发邮件告警，并以退出码 4 结束；日志追加到 `~/.opssop/offsite.log`，每次操作报告写入 `OFFSITE_REPORT_DIR`。
- 依赖：宿主需要 `docker`、`ssh`、`scp` 与 Python 3.10+ 标准库；不需要安装任何第三方包。

配置写在环境文件的 `OFFSITE_*` 键里（命令行与环境变量优先）：

| 键 | 说明 |
|---|---|
| `OFFSITE_COMPOSE_PROJECT` | Compose 项目名，用于定位运行平台备份的后端容器 |
| `OFFSITE_CONTAINER` / `OFFSITE_BACKUP_DIR` | 直接指定容器名与容器内备份目录 |
| `OFFSITE_HOST` / `OFFSITE_USER` / `OFFSITE_REMOTE_DIR` | 异地主机、SSH 用户与远端绝对路径目录 |
| `OFFSITE_PORT` / `OFFSITE_IDENTITY` | SSH 端口与私钥路径 |
| `OFFSITE_KEEP` | 异地保留份数（默认 14） |
| `OFFSITE_MAX_AGE_HOURS` | 备份年龄上限（默认 30 小时） |
| `OFFSITE_STATE_FILE` / `OFFSITE_REPORT_DIR` | 幂等状态文件与报告目录 |

调度示例（Docker 引擎所在宿主机；本项目为 WSL，用户级 crontab 即可，无需 root）：

```bash
crontab -e
# 每小时 17 分检查一次：当天备份只复制一次，失败会自动重试并告警
17 * * * * cd /opt/opssop && python3 backend/scripts/offsite_daily.py --env-file .env.production >> ~/.opssop/offsite.log 2>&1
```

宿主休眠或关机时会错过执行点，恢复后下一个整点会补上（同一天同一备份只复制一次）。

平台生成的归档可以直接用本文的恢复流程：

```bash
python3 backend/scripts/ops_cli.py restore \
  --project opssop-prod --env-file .env.production \
  --compose-file docker-compose.yml --compose-file docker-compose.prod.yml \
  --archive /data/OpsSop/BackUp/opssop-<时间>.dump \
  --metadata /data/OpsSop/BackUp/opssop-<时间>.json \
  --target-db opssop_restore_check --run-id restorecheck \
  --out-dir /var/backups/opssop-restore-check
```

## 30 天保留口径

30 天保留**只作用于备份文件、审计/登录记录和（可选的）应用运行日志**，不作用于知识库正文：

- 备份：平台自动删除超过 30 天的 `.dump` / `.json` 对（含没有配对元信息的孤立归档）。
- 审计与登录尝试：平台每日清理超过 30 天的记录。
- 应用运行日志：设置 `LOG_FILE_ENABLED=true` 后按天轮转保留 `LOG_RETENTION_DAYS`（默认 30）天；未启用时由宿主 logrotate 或日志平台负责。
- **业务知识数据（故障、案例、SOP）永久保留**：它们是知识资产，按 30 天字面清理会清空知识库、破坏长期检索价值。确需清理时实现"超过期限归档 + 管理员审批 + 可导出"，不做定时物理删除。

## 备份范围

| 包含 | 不包含 |
|---|---|
| 应用 schema、业务数据、账号哈希、序列 | PostgreSQL 集群级角色与角色口令 |
| Alembic 版本、pgvector 扩展及索引数据 | `pg_hba.conf`、`postgresql.conf`、宿主及网络配置 |
| 配套计数、状态分布、业务摘要与校验信息 | 环境文件、模型密钥、TLS 证书及外部模型配置 |

私有环境配置、角色配置和证书应单独安全保存。数据库归档包含业务内容及账号哈希，须限制访问，不提交版本库或公开存储。

备份前安排维护窗口并暂停应用写入。工具的核对快照与 `pg_dump` 分别采集，持续写入可能导致恢复后的快照与元信息不同。维护时可停止后端与前端，保留数据库服务：

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml stop backend frontend
```

## 创建备份

以下 PowerShell 7 示例生成独立运行 ID 和输出目录。归档与元信息路径供后面的恢复命令使用；重新打开终端时，应填写已保存的实际路径。

```powershell
$backupRun = 'backup' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$backupDir = Join-Path $env:USERPROFILE "opssop-backups\$backupRun"
$archive = Join-Path $backupDir "opssop-$backupRun.dump"
$metadata = Join-Path $backupDir "opssop-$backupRun.json"
python backend/scripts/ops_cli.py backup `
  --project opssop-prod `
  --env-file .env.production `
  --compose-file docker-compose.yml `
  --compose-file docker-compose.prod.yml `
  --run-id $backupRun `
  --out-dir $backupDir `
  --name "opssop-$backupRun.dump"
```

Linux 示例，输出父目录须预先具备写入权限：

```bash
BACKUP_RUN="backup$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
BACKUP_DIR="/var/backups/opssop/$BACKUP_RUN"
ARCHIVE="$BACKUP_DIR/opssop-$BACKUP_RUN.dump"
METADATA="$BACKUP_DIR/opssop-$BACKUP_RUN.json"
python3 backend/scripts/ops_cli.py backup \
  --project opssop-prod \
  --env-file .env.production \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.prod.yml \
  --run-id "$BACKUP_RUN" \
  --out-dir "$BACKUP_DIR" \
  --name "opssop-$BACKUP_RUN.dump"
```

工具使用 `pg_dump -Fc` 创建归档，拒绝覆盖同名 `.dump` 或 `.json`。归档非空、`pg_restore -l` 可读、SHA-256 与大小生成、元信息落盘且临时文件清理完成后，才返回完整成功。

保存以下产物：

- `opssop-<run_id>.dump`：数据库归档。
- `opssop-<run_id>.json`：归档大小、SHA-256、来源数据库与工具版本、Alembic / pgvector 版本、来源快照和代码信息。
- `backup-result.json`：操作结果与清理状态。

归档和元信息必须成对保存。取消或失败返回非零；可捕获的用户取消返回 `130`。容器临时文件清理失败时，归档可能有效，但结果为 `cleanup_incomplete`、`core_success=true`、`cleanup_complete=false`，退出 `4`，应完成收尾后再关闭工单。

若备份后继续运行原业务，使用相同项目、环境和 Compose 参数执行 `start backend frontend`，再检查服务状态。升级或灾难恢复期间按维护计划保持暂停写入。

## 定时备份与保留

定时任务使用 `backend/scripts/backup_scheduled.py` 包装上面的命令：同一时刻只允许一个实例，成功后保留最近 N 份（默认 14），并写入 `backup.log`。它只依赖标准库与 Docker CLI，失败返回非零，便于监控告警。

Linux（cron，每天 02:30）：

```cron
30 2 * * * cd /opt/opssop && python3 backend/scripts/backup_scheduled.py \
  --project opssop-prod --env-file .env.production \
  --out-dir /var/backups/opssop --keep 14 >> /var/log/opssop-backup.log 2>&1
```

systemd timer 示例（`opssop-backup.service` + `opssop-backup.timer`）与 cron 等价，适合需要开机补跑和更清晰状态管理的服务器。Windows 生产预演可用任务计划程序调用同一命令；输出目录放在仓库之外。

定时任务上线前至少验证一次：

1. 手动执行同一条命令，确认退出码为 0 且生成 `.dump` 与元信息。
2. 用[恢复到全新目标库](#恢复到全新目标库)恢复一份归档并核对业务数量。
3. 模拟一次失败（例如错误项目名），确认日志与返回码能被监控捕获。

保留策略建议至少 14 天，并在版本升级、月度节点额外归档；备份包含业务内容与账号哈希，必须限制访问。需要异地保存时，先加密再传输，不把归档提交到版本库或公共对象存储。


## 恢复到全新目标库

恢复工具只创建新库，不覆盖来源或既有目标。默认要求来源与目标 PostgreSQL 主版本一致，并核对归档与完整元信息；跨主版本迁移应单独制定方案，不作为常规恢复步骤。

PowerShell 7，沿用上面保存的 `$archive` 和 `$metadata`：

```powershell
$restoreRun = 'restore' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$restoreDir = Join-Path $env:USERPROFILE "opssop-restore\$restoreRun"
python backend/scripts/ops_cli.py restore `
  --project opssop-prod `
  --env-file .env.production `
  --compose-file docker-compose.yml `
  --compose-file docker-compose.prod.yml `
  --run-id $restoreRun `
  --archive $archive `
  --metadata $metadata `
  --target-db "opssop_restore_$restoreRun" `
  --out-dir $restoreDir
```

Linux，沿用上面的 `$ARCHIVE` 和 `$METADATA`：

```bash
RESTORE_RUN="restore$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
python3 backend/scripts/ops_cli.py restore \
  --project opssop-prod \
  --env-file .env.production \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.prod.yml \
  --run-id "$RESTORE_RUN" \
  --archive "$ARCHIVE" \
  --metadata "$METADATA" \
  --target-db "opssop_restore_$RESTORE_RUN" \
  --out-dir "/var/backups/opssop/restore/$RESTORE_RUN"
```

### 执行顺序与保护

1. 校验元信息格式、来源标识、快照结构、归档存在性、大小与 SHA-256。
2. 校验目标名称、目标不存在、目标不同于来源，以及 PostgreSQL 主版本。
3. 在容器内确认归档可读后，创建全新目标库。
4. 使用 `pg_restore --no-owner --no-privileges --exit-on-error` 恢复。
5. 比对表计数、状态分布、业务摘要、Alembic 与 pgvector 版本，写入恢复报告。

目标名只能包含安全的 PostgreSQL 标识符字符。工具拒绝既有库（包括空库）、来源库、系统与模板库、保留的应用数据库名，以及测试或评测命名空间；不提供自动覆盖、清空或 `--clean` 开关。

恢复报告为 `<out-dir>/<target-db>-restore.json`。恢复或核对失败时，默认保留本次创建的目标库以便诊断。只有显式 `--cleanup-failed-target` 才尝试清理本次创建的失败目标，不处理启动前已存在的库。临时文件清理失败仍以非零退出并记录 `cleanup_incomplete`。

逻辑归档不包含集群角色。迁移到其他集群前，应准备登录角色、连接权限及所需 schema 权限；恢复后核对业务表属主与授权，不能依靠归档自动重建整个集群权限。

## 恢复后接入业务

工具完成数据库恢复后不会自动切换生产流量。保留原配置，并先用独立应用实例连接恢复库：数据库主机指向承载恢复库的 PostgreSQL 实例，`POSTGRES_DB` 使用报告中的 `target_database`，其余凭据按目标集群设置。使用 Compose 时，通过额外覆盖文件设置 `backend.environment.POSTGRES_HOST` 和数据库连接字段，并将后端接入目标数据库可达的容器网络；仅在环境文件新增 `POSTGRES_HOST` 不会覆盖基础 Compose 中的 `db`。独立实例需使用单独项目、端口和环境文件，不重新初始化或复用生产命名卷。

检查以下内容后，再安排正式切换：

- 登录、角色权限及工作台数据正常。
- 正式检索仅包含已审核案例和当前有效已发布 SOP；无权及未发布材料不作为正式依据。
- 可以创建故障、追加证据、人工确认、审核和发布，主键序列与审计记录能够续写。
- 恢复库的应用版本与 Alembic 版本匹配，启动迁移符合计划。
- embedding 配置与备份索引兼容；配置变化时按“需要重建／关键词降级”处理，不混用旧向量。

需要重建索引时，由管理员在已指向正确目标库的后端容器执行 `python scripts/build_embedding_index.py --rebuild`。启用 embedding 后，该操作会向配置的模型服务发送脱敏后的授权知识文本；先核对服务配置与数据边界。

周期性隔离演练见[运维指南](operations.md)。备份成功仅证明归档生成，定期在新库恢复并执行真实业务续办，才能确认恢复流程可用。

## 升级与回退

升级前保存配套归档、元信息、应用版本、镜像及私有配置。在全新库恢复备份后，再按发行版本支持的 Alembic 路径升级。需要回退时，切回兼容的旧应用镜像，并按计划使用旧库或恢复到新库的旧备份；单独回滚镜像不会回滚数据库。

保留期限、加密、离机副本、自动调度及过期删除由部署方的备份策略管理。操作归档或数据库前核对确切目标，避免以通配清理替代保留策略。

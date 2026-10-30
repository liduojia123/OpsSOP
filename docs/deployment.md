# OpsSOP 3.5.1 部署指南

OpsSOP 采用 Vue 前端、FastAPI 后端与 PostgreSQL / pgvector 数据库，通过 Docker Compose 部署。应用与模型服务独立配置；关闭模型时，故障录入、案例审核、SOP 管理和关键词检索仍可使用。

## 环境要求

- Docker 引擎及 Docker Compose 2.24.4 或更高版本，支持 `!reset`、`!override` 合并标签。
- Linux 服务器，或能够运行 Linux 容器的 Windows Docker Desktop / WSL Docker 环境。命令须在能够访问 Docker 引擎的同一环境执行。
- 构建时能够获取 Python、Node.js、nginx、pgvector 基础镜像及项目依赖；应用运行不要求 GPU。
- 生产域名、TLS 证书、反向代理、入口防火墙和数据存储由部署方准备。

本文命令从 OpsSOP 发行源码根目录执行。生产 Compose 项目统一使用 `opssop-prod`，默认命名卷为 `opssop-prod_pgdata`。已有安装必须沿用原项目名、数据卷和环境文件；更换项目名会创建另一套资源。

## 准备生产配置

首次安装时复制示例；已有安装直接维护原环境文件，不覆盖已有口令和配置。

PowerShell 7：

```powershell
Copy-Item .env.production.example .env.production
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Linux：

```bash
cp .env.production.example .env.production
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
chmod 600 .env.production
```

分别生成数据库口令、会话密钥和管理员口令，将其填入 `.env.production`。该文件包含私有配置，须限制访问且不提交版本库。不要沿用示例值；生产环境不使用演示数据库或演示账号。

| 配置 | 生产设置 |
|---|---|
| `POSTGRES_USER` / `POSTGRES_DB` | 应用数据库用户和数据库名；示例数据库为 `opssop_prod` |
| `POSTGRES_PASSWORD` | 随机数据库口令 |
| `SECRET_KEY` | 随机会话密钥，至少 32 字符 |
| `OPSSOP_ENV` | `production` |
| `OPSSOP_ADMIN_USERNAME` | 首次初始化管理员用户名 |
| `OPSSOP_ADMIN_PASSWORD` | 至少 12 字符，且不使用示例口令 |
| `OPSSOP_SEED_DEMO` | `0` |
| `AI_MODE` / `EMBEDDING_MODE` | 默认 `disabled`，按实际服务单独配置 |
| `LOCAL_ONLY` | 默认 `1`；使用云模型前显式改为 `0` |
| `FRONTEND_PORT` | 默认 `8080`，生产入口绑定 `127.0.0.1` |
| `TRUSTED_PROXY_CIDRS` | 可信反向代理网段，逗号分隔 CIDR；Docker 默认 `172.16.0.0/12`，留空则忽略转发头 |
| `BACKUP_HOST_DIR` | 宿主机备份目录，默认 `/data/OpsSop/BackUp`，挂载到后端容器同路径 |
| `BACKUP_ENABLED` / `BACKUP_HOUR` | 生产默认启用；`BACKUP_HOUR` 为容器本地时间（默认 `2`，即每天 02:00） |
| `BACKUP_RETENTION_DAYS` | 备份保留天数，默认 `30` |
| `AUDIT_RETENTION_DAYS` | 审计与登录尝试保留天数，默认 `30`；业务知识数据不自动删除 |
| `EMAIL_MODE` | 默认 `disabled`；设为 `smtp` 可发送备份告警。`EMAIL_PORT=465` 时配 `EMAIL_USE_SSL=true`，587 用 `EMAIL_USE_TLS=true` |
| `ALERT_EMAIL_TO` | 备份失败/超过 26 小时无成功备份时的告警收件人，逗号分隔；留空不发送 |
| `LOG_FILE_ENABLED` | 默认 `false`（仅 stdout）。`true` 时应用日志按天轮转写入 `LOG_HOST_DIR`，保留 `LOG_RETENTION_DAYS` 天（默认 30） |

管理员初始化配置不会自动轮换数据库中已有账号的口令；已有账号通过应用账号管理流程维护。修改 `POSTGRES_PASSWORD` 也不会修改既有数据卷中的 PostgreSQL 角色口令，应与数据库角色变更同步实施。

启用 `cloud` 聊天或 embedding 后，脱敏后的输入、查询和授权知识片段会发送到配置的外部模型服务。`local` 指向容器可达的受信任本机或内网接口，不允许失败后自动切换云服务。`LOCAL_ONLY=1` 是应用配置策略，严格隔离还需部署层控制出口。

## 构建与启动

保留生产覆盖文件，确保数据库和后端不发布宿主端口，前端只绑定回环地址：

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml config --quiet

docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build

docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml ps
```

Linux 使用相同参数，将 PowerShell 续行符替换为 `\`，或将命令写为一行。`config --quiet` 只检查配置，不打印展开后的口令；不要把包含私有环境变量的完整 Compose 配置或日志上传到公共渠道。

生产合并配置应具备以下属性：

- `db.ports` 与 `backend.ports` 为空；`backend.volumes` 不挂载测试目录。
- `frontend.ports` 的宿主地址为 `127.0.0.1`。
- 后端设置 `OPSSOP_ENV=production`、`OPSSOP_SEED_DEMO=0`，各服务保留健康检查。
- 数据库、后端、前端设置 `restart: unless-stopped`；容器日志按 10MB × 5 轮转，避免磁盘被日志占满。

后端启动顺序为配置预检、Alembic 迁移、幂等初始化、启动 API。配置不合规、迁移失败或检测到遗留不安全活动账号时，后端退出。新库只初始化管理员和系统分类，不自动导入演示内容。

如迁入数据库中存在遗留演示账号，可先通过一次性容器查看问题，再显式处理指定账号：

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend `
  python scripts/prepare_production.py
```

该命令列出账号问题；显式停用账号时增加 `--disable <用户名>`。处理前核对业务账号用途，并在完成后重新启动后端。

## 配置 HTTPS 入口

宿主 nginx、Caddy 或负载均衡器负责 TLS 终止，转发至 `http://127.0.0.1:8080`，端口与 `FRONTEND_PORT` 保持一致。容器内 nginx 负责静态页面、SPA 路由及同源 `/api/` 代理。

- 保留 `/api/` 路径、`Host`、`X-Real-IP`、`X-Forwarded-For` 等转发信息，并为模型调用配置合适的请求超时。
- 仅按部署需要开放 HTTPS 入口，不对公网发布 PostgreSQL 或后端端口。
- 管理证书申请与续期、DNS 和防火墙；在目标环境检查证书链、登录、权限和业务闭环。
- 容器中的 `localhost` 指向容器自身。Windows Docker Desktop 访问宿主模型通常使用 `host.docker.internal`；Linux 使用容器能够访问的宿主或内网地址。

部署完成后可从宿主访问 `http://127.0.0.1:8080/api/system/health`，并通过配置的 HTTPS 域名进行登录及业务操作。应用健康检查只说明服务可响应，不代表目标环境的证书、备份或模型效果已确认。

## 平台内置备份

生产模式会启动平台内的每日维护任务：按容器本地时间 `BACKUP_HOUR` 执行 `pg_dump`，写入 `BACKUP_DIR`（生产挂载到宿主机 `BACKUP_HOST_DIR`，默认 `/data/OpsSop/BackUp`）。备份文件与元信息成对保存，权限为 `600`，默认保留 `BACKUP_RETENTION_DAYS=30` 天。

管理员在「系统管理 → 数据备份」可以查看备份列表、手动触发和下载。手动触发与定时任务使用同一进程锁；单实例部署下不会并发写同一目录。首次启动若已过当天执行时间且当天没有备份，会在约 60 秒后补跑一次。

```bash
# 宿主机准备备份目录（不要放在数据卷内部，也不要提交到仓库）
sudo install -d -m 700 -o root -g root /data/OpsSop/BackUp
```

备份目录包含业务数据与账号哈希，必须限制访问并按需加密后异地保存；平台下载入口只对管理员开放并写入审计。业务知识数据（故障、案例、SOP）**不会**因为 30 天保留策略被自动删除；30 天策略作用于备份文件和审计/登录记录。若后续需要清理业务数据，应单独实现归档与审批，不能在定时任务里直接删除。

容器运行日志由 Docker 按 10MB × 5 轮转，不是按天保留；不能依赖容器 json-file 驱动满足 30 天留存。两种方式任选其一：

- 平台内置文件日志（推荐，无需宿主配置）：设置 `LOG_FILE_ENABLED=true`，应用日志按天轮转写入挂载目录 `LOG_HOST_DIR`（默认 `/data/OpsSop/Logs`），自动清理超过 `LOG_RETENTION_DAYS`（默认 30）天的旧文件。
- 宿主机 logrotate 或日志平台（Loki/Vector 等）采集容器 stdout。

审计记录的 30 天清理由平台维护任务执行。

## 反向代理与真实客户端地址

登录限流按“账号 + 来源地址”计数，审计与排障也需要真实来源地址。后端默认忽略转发头，只有请求来自 `TRUSTED_PROXY_CIDRS` 中的代理时才采信 `X-Forwarded-For` / `X-Real-IP`，并从代理链右侧向左跳过可信代理，取第一个不可信地址。

链路要求：

1. 宿主反向代理把客户端地址写入 `X-Forwarded-For`（或至少 `X-Real-IP`），再转发到 `127.0.0.1:8080`。
2. 容器内 Nginx 使用 `$proxy_add_x_forwarded_for` 追加地址；后端只信任容器网络与显式配置的网段。
3. 如果宿主代理没有传递转发头，后端只能看到 Docker 网关地址，多个用户会共享同一来源限流额度。

```nginx
# 宿主 HTTPS 入口示例：在边缘覆盖客户端自带的 X-Forwarded-For，防止伪造
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
```

如果最外层还有 CDN 或负载均衡器，应只在确认其出口网段可信的层使用 `$proxy_add_x_forwarded_for`，并在 `TRUSTED_PROXY_CIDRS` 中加入该网段；不要在直接面向公网的入口盲目追加客户端提供的转发头。修改后可用登录限流记录或审计来源验证一次真实客户端地址。

容器内 Nginx 已为页面设置 CSP、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`，并让带哈希的静态资源长期缓存、`index.html` 不缓存。HSTS 与证书由宿主 TLS 入口负责；如果前端嵌入到其他站点（例如监控门户 iframe），需要按实际来源调整 CSP 的 `frame-ancestors`，默认不允许被嵌入。

生产备份定时、监控告警与上线判定见[生产上线检查清单](production-checklist.md)；备份命令与恢复演练见[备份与恢复](backup-restore.md)。

## 升级与回退

1. 安排维护窗口，暂停业务写入，按[备份与恢复](backup-restore.md)保存归档及元信息，记录当前应用版本、镜像标识和环境配置。
2. 在全新目标库恢复备份并核对后，选定固定发行标签。首次部署使用 `v3.5.1`，后续升级使用对应发行标签，避免使用浮动分支作为生产版本。
3. 在不覆盖私有环境文件的情况下更新源码。沿用生产项目名和数据卷，执行上述 `up -d --build`；后端按发行迁移自动升级数据库。
4. 确认健康状态、登录、权限、检索和关键业务操作后恢复流量，保留升级前备份。

固定发行版本的操作示例，升级时将 `v3.5.1` 替换为选定的目标标签：

```powershell
git fetch --tags origin
git checkout --detach v3.5.1
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

切换版本前确认部署目录没有需要保留的源码修改；私有环境文件不纳入版本控制。

升级前需保留可重新运行的旧版本镜像。回退应同时考虑应用与数据库兼容性：必要时恢复旧备份到新库，再将旧版本应用指向该库。只回滚镜像不会回滚数据库；数据库降级需先在副本上评估，不支持任意版本的无损降级。

停止服务可使用同一组项目、环境和 Compose 参数执行 `stop`。数据保存在命名卷中；日常启停、升级和故障处理不删除数据卷。

## 运行边界

当前部署方式面向单实例应用；模型限流与并发计数位于进程内。多实例、统一限流、自动异地容灾、备份调度与保留策略由部署方案补充。模型接入、数据库持久化、目标服务器网络与 HTTPS 应分别管理，各自的可用状态互不替代。

登录限流使用 PostgreSQL 事务锁及共享记录，后端进程间共享；默认账号每 60 秒 10 次、直接连接来源每 60 秒 120 次。随版 Nginx 对登录入口设置 60 次/分钟、突发 20 次的来源限流。反向代理后来源可能是代理地址，多个用户会共用来源额度；公网部署应在最外层可信 HTTPS 入口按真实客户端地址配置登录限流。如果上游还有 CDN 或负载均衡器，必须只信任已明确配置的上游地址，不能直接信任任意客户端提供的转发头。

本版增加会话撤销和登录限流表，升级前备份数据库并执行迁移；回退前按[备份恢复](backup-restore.md)流程评估恢复。修改密码会使该账号所有已签发会话失效，退出仅撤销当前令牌。已在途的请求可能在撤销前完成。回退到不检查撤销表的旧版时，已退出但未过期的令牌可能重新有效；回退时轮换 `SECRET_KEY`，让所有会话重新登录。

例如，若宿主机 Nginx 直接接收客户端 HTTPS 请求，可在其 `http` 块和对应 HTTPS `server` 块中分别配置：

```nginx
# http 块中
limit_req_zone $binary_remote_addr zone=opssop_public_login:10m rate=30r/m;

# 已启用 TLS 的 server 块中；其他路径仍按现有反向代理规则处理
location = /api/auth/login {
    limit_req zone=opssop_public_login burst=10 nodelay;
    limit_req_status 429;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_pass http://127.0.0.1:8080;
}
```

这是直接接入场景的示例。部署方应先确认 TLS、端口和上游代理拓扑；如果存在 CDN，只有配置受信任的 CDN 地址并正确还原客户端地址后，才能按客户端限流。容器内部与后端仍可能看到宿主代理地址，因而共享额度，须根据实际并发评估。

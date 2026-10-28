# OpsSOP 1.1.0 部署指南

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

## 升级与回退

1. 安排维护窗口，暂停业务写入，按[备份与恢复](backup-restore.md)保存归档及元信息，记录当前应用版本、镜像标识和环境配置。
2. 在全新目标库恢复备份并核对后，选定固定发行标签。首次部署使用 `v1.1.0`，后续升级使用对应发行标签，避免使用浮动分支作为生产版本。
3. 在不覆盖私有环境文件的情况下更新源码。沿用生产项目名和数据卷，执行上述 `up -d --build`；后端按发行迁移自动升级数据库。
4. 确认健康状态、登录、权限、检索和关键业务操作后恢复流量，保留升级前备份。

固定发行版本的操作示例，升级时将 `v1.1.0` 替换为选定的目标标签：

```powershell
git fetch --tags origin
git checkout --detach v1.1.0
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

切换版本前确认部署目录没有需要保留的源码修改；私有环境文件不纳入版本控制。

升级前需保留可重新运行的旧版本镜像。回退应同时考虑应用与数据库兼容性：必要时恢复旧备份到新库，再将旧版本应用指向该库。只回滚镜像不会回滚数据库；数据库降级需先在副本上评估，不支持任意版本的无损降级。

停止服务可使用同一组项目、环境和 Compose 参数执行 `stop`。数据保存在命名卷中；日常启停、升级和故障处理不删除数据卷。

## 运行边界

当前部署方式面向单实例应用；模型限流与并发计数位于进程内。多实例、统一限流、自动异地容灾、备份调度与保留策略由部署方案补充。模型接入、数据库持久化、目标服务器网络与 HTTPS 应分别管理，各自的可用状态互不替代。

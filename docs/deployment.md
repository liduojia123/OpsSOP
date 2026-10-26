# 部署配置与本地 Linux 容器验证

## 1. 三种用法

| 用法 | 命令/配置 | 端口/数据 |
|---|---|---|
| 本地开发 | `docker compose up -d --build` | DB/后端仅回环;前端 `0.0.0.0:8080`;可挂载 tests |
| 演示 | `docker compose -p opssop-demo --env-file .env.demo ...` | 独立项目/卷/端口;demo 内容显式 seed |
| 生产 | `docker compose -p opssop-prod --env-file .env.production -f docker-compose.yml -f docker-compose.prod.yml up -d --build` | DB/后端无宿主端口;前端仅 `127.0.0.1`;无 tests 挂载 |

生产示例从 `.env.production.example` 复制为 `.env.production`;后者被 Git 忽略。
不要复用开发 `.env`、`mimo_pgdata` 或演示账号。

## 2. 生成合成/真实部署密钥

```powershell
# PowerShell 7:生成 32+ 字节随机值,人工填入私有 .env.production
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
# Linux
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 或
openssl rand -hex 32
```

生产启动前检查(在 Alembic 迁移之前):

- `OPSSOP_ENV=production`;
- `OPSSOP_SEED_DEMO=0`;
- `OPSSOP_ADMIN_PASSWORD` 非示例默认口令且长度 ≥ 12;
- `SECRET_KEY` 非示例默认值且长度 ≥ 32;
- `AI_MODE`/`EMBEDDING_MODE` 配置合法;默认 disabled。

检查由 `backend/scripts/preflight.py` 执行,`backend/scripts/start.sh` 的顺序为:
配置预检 → `alembic upgrade head` → `ensure_seed`(含遗留不安全账号检查)→ uvicorn。
任一阶段失败,容器退出,API 不进入可用状态。

## 3. 检查合并后的真实配置

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml config
```

必须确认:

- `db.ports` 为空,`backend.ports` 为空;
- `backend.volumes` 不含 `backend/tests`;
- `frontend.ports` 的 `host_ip` 为 `127.0.0.1`;
- 后端环境 `OPSSOP_ENV=production`、`OPSSOP_SEED_DEMO=0`;
- 数据库/后端/前端健康检查仍存在。

`docker-compose.prod.yml` 使用 `!reset`/`!override`,不是简单写成 `ports: []`;
已在本机 Docker Compose v5.6 用 `docker compose config --format json` 核对合并结果。
旧版 Compose 若不支持这些标签,请升级 Compose 或改用等价的独立生产文件,不能仅凭
`ports: []` 假定端口已移除。

## 4. 启动与 smoke

```powershell
docker compose -p opssop-prod --env-file .env.production `
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

```bash
# 本机/服务器回环
curl -fsS http://127.0.0.1:8080/api/system/health
curl -fsS http://127.0.0.1:8080/ | head
```

- 前端 nginx 同源 `/api/` 代理到 `backend:8000`,静态资源/SPA 路由由 nginx 提供。
- 后端不直接暴露公网;数据库不发布宿主端口。
- 生产模式不自动创建 operator/reviewer,不自动导入 demo 内容;
  正常空库只应有管理员和系统分类。

### 负例(必须失败)

- 弱 `OPSSOP_ADMIN_PASSWORD` / 示例 `SECRET_KEY` / `OPSSOP_SEED_DEMO=1`:
  预检失败,`alembic_version` 不应被创建;
- 既有演示账号(operator/reviewer 种子指纹):迁移后 seed 检查失败,API 不启动;
- 迁移权限/连接失败:容器非零退出,API 不可用。

## 5. HTTPS 与网络边界

- 本仓库只把前端发布到 `127.0.0.1`;HTTPS 由宿主 nginx/Caddy/负载均衡等反向代理终止,
  证书申请/续期、DNS、防火墙不在本批自动配置范围。
- 反向代理需保留现有 nginx 的 `/api/` 路径、`Host`、`X-Real-IP`、`X-Forwarded-For`
  和足够超时;未实测证书链时不能写“HTTPS 已验证”。
- 容器内 `localhost` 指容器自身:
  - Windows Docker Desktop 访问宿主机模型服务通常用 `host.docker.internal`;
  - Linux 服务器应使用宿主机可达 IP/内网地址,不能填用户 Windows 电脑的本地地址。
- `LOCAL_ONLY=1` 是应用级禁止 cloud 聊天/embedding 的策略,不是网络层出口控制;
  严格隔离需在部署层限制出站。
- 单实例部署;AI/embedding 限流是进程内计数,多实例需外部限流。
  本批不扩展多实例/Kubernetes/Redis/定时备份平台。

## 6. 本地 Linux 容器验证范围

本批在 Windows Docker Desktop 的 Linux 容器中完成:

- 已有依赖镜像 + 当前源码挂载运行当前 `start.sh`/App/nginx 配置;
- 备份恢复、恢复库 HTTP 续办、命名卷跨容器重建、生产正反例和 nginx 同源代理 smoke。

这不等于:

- 从零构建新镜像成功(依赖镜像/网络条件另记);
- 目标云服务器、公网 HTTPS、真实 DNS/防火墙已验收;
- 真实聊天模型/embedding/真人标签已验证。

## 7. 升级、备份与回退

- 升级前先按[备份与恢复](backup-restore.md)备份,并记录 SHA-256/元信息。
- 迁移由 `start.sh` 自动执行;失败时容器退出,不要强行把旧进程留在可用状态。
- 回退路径:切回旧应用镜像 + 旧库/已验证备份;镜像回滚不等于数据库回滚。
- 迁移前准备可在新库恢复验证,再按受支持 Alembic 路径升级;不承诺任意版本降级。

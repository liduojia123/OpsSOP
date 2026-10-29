# 生产上线检查清单

适用于单实例自托管部署（当前版本约 6–20 名用户，AI 可先关闭）。逐项确认后再对真实用户开放；未完成的项必须明确记录风险与负责人。

## 1. 发布基线

- [ ] 部署目录固定在明确的 Git tag 或提交，不使用移动中的分支。
- [ ] 前端、后端镜像按版本打标签并保留旧镜像，便于回滚。
- [ ] 记录本次版本、镜像 ID、数据库迁移版本与配置变更。
- [ ] 发布前在隔离环境执行：前端构建与测试、后端 pytest、Compose 配置检查。

```bash
cd frontend && npm ci && npm run build && npm test
cd ../backend && python -m pytest tests -q
cd .. && docker compose -p opssop-prod --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml config --quiet
```

## 2. 配置与密钥

- [ ] `.env.production` 权限为 `600`，只保存在部署机，不进入版本库或聊天工具。
- [ ] `POSTGRES_PASSWORD`、`SECRET_KEY`、管理员口令均为随机值，不使用示例值。
- [ ] `OPSSOP_ENV=production`、`OPSSOP_SEED_DEMO=0`，配置预检通过。
- [ ] `AI_MODE` / `EMBEDDING_MODE` 按决策设置；未获授权时保持 `disabled`。
- [ ] `TRUSTED_PROXY_CIDRS` 只包含实际反向代理所在网段（Docker 部署默认 `172.16.0.0/12`）。
- [ ] 变更数据库口令时同步修改 PostgreSQL 角色，不能只改环境文件。

## 3. 网络与 TLS

- [ ] 只对外开放 443；数据库、后端端口不发布到宿主；前端只绑定 `127.0.0.1`。
- [ ] 宿主反向代理终止 TLS，证书有效期与自动续期已验证。
- [ ] 反代保留 `/api/` 路径，并传递 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto`。
- [ ] 外层代理配置 HSTS；容器内 Nginx 已带 CSP、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`。
- [ ] 访问域名确认：静态资源长缓存、`index.html` 不缓存、gzip 生效。

```bash
curl -sSI https://<域名>/ | grep -Ei 'content-security|x-frame|referrer|strict-transport'
curl -sS https://<域名>/api/system/health
```

## 4. 数据保护

- [ ] 已启用平台内置备份（`BACKUP_ENABLED=true`），`BACKUP_HOST_DIR=/data/OpsSop/BackUp` 可写，并至少成功执行一次。
- [ ] 已从备份恢复到**新库**并核对业务数量、审核状态与检索结果。
- [ ] 备份保存在仓库与数据库主机之外，访问受控；需要时加密后异地保存。
- [ ] 保留策略明确：备份与审计/登录记录保留 30 天；故障、案例、SOP 业务知识不自动删除；磁盘余量足够。
- [ ] 运行日志 30 天留存由宿主 logrotate 或日志平台保证；容器 json-file 仅按大小轮转。
- [ ] 恢复目标时间（RTO）与可接受数据丢失（RPO）已与使用方确认。

## 5. 运行韧性

- [ ] Compose 服务设置 `restart: unless-stopped`，容器日志限制为 10MB × 5。
- [ ] 数据库、后端、前端健康检查均为 healthy；重启后端后自动恢复。
- [ ] 磁盘、内存、CPU 余量满足当前规模；数据库连接数未接近上限。
- [ ] 容器时区与主机一致，日志时间可对应。

```bash
docker compose -p opssop-prod --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml ps
docker stats --no-stream
df -h
```

## 6. 监控与告警

- [ ] 外部监控每 1–5 分钟检查 `https://<域名>/api/system/health`（含数据库连通）。
- [ ] 监控首页与登录页可用性、5xx 比例、容器重启次数。
- [ ] 告警覆盖：服务不可用、磁盘不足、备份失败或过期、证书剩余天数、异常登录失败量。
- [ ] 日志可在宿主机查看并保留至少 7 天；不包含口令或令牌。

可用方案：Uptime Kuma / 云监控黑盒拨测 + 宿主机 `cron` 检查；Prometheus 不是当前版本的必需依赖。

## 7. 升级与回滚

- [ ] 升级前完成数据库备份，并保留当前可运行镜像。
- [ ] 已阅读目标版本的迁移说明；数据库迁移在预演库验证。
- [ ] 回滚方案明确：应用回滚到旧镜像；数据库回滚只在副本上评估，不直接降级生产库。
- [ ] 回滚后轮换 `SECRET_KEY` 的步骤已记录（旧令牌风险）。

参考[部署指南](deployment.md#升级与回退)。

## 8. 业务验收

- [ ] 三种角色登录：运维、审核、管理员；无权限角色看不到对应入口。
- [ ] 故障创建 → 追加记录 → 人工确认解决 → 生成案例 → 审核 → 生成/发布 SOP → 检索复用。
- [ ] AI 关闭时：助手与检索入口明确提示未启用，人工流程不受影响。
- [ ] 并发保护生效：两名用户先后编辑同一案例/SOP，不会互相覆盖（428/409 提示清晰）。
- [ ] 审计记录可按操作人、时间、事件筛选，分页可用。
- [ ] 修改密码后旧会话失效；退出登录后令牌不可复用。

## 9. 已知边界（上线时向使用方说明）

- 单实例部署；模型限流与并发计数在进程内（AI 关闭时不涉及）。
- 未集成 SSO/LDAP、双因素认证；账号由管理员分配。
- 只接收文本，不接收日志文件附件。
- 健康检查只表示服务可响应，不代表证书、备份或模型效果已验收。
- 未定义多机房容灾与托管 SLA。

## 上线判定

全部 P0/数据保护/回滚项完成后可上线；监控、性能与体验项允许在上线后一周内补齐，但必须有明确负责人和截止时间。

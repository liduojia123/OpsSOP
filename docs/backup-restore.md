# 备份与恢复

工具入口:`backend/scripts/ops_cli.py`。只依赖 Python 标准库和 `docker` CLI;
备份/恢复在数据库容器内通过本地 socket 执行,不把密码放进命令行,也不经过
PowerShell 文本管道或 `Get-Content`/`Set-Content` 传递归档。

## 1. 备份

```powershell
# PowerShell 7。先确认项目/环境文件/输出目录均为本轮目标。
$run = "backup20261022a"
python backend/scripts/ops_cli.py backup `
  --project opssop-demo-p4b1 `
  --env-file .env.demo `
  --compose-file docker-compose.yml `
  --run-id $run `
  --out-dir "D:\opssop-backups" `
  --name "opssop-$run.dump"
```

```bash
# Linux
RUN_ID=backup20261022a
python3 backend/scripts/ops_cli.py backup \
  --project opssop-demo-p4b1 \
  --env-file .env.demo \
  --compose-file docker-compose.yml \
  --run-id "$RUN_ID" \
  --out-dir /var/backups/opssop \
  --name "opssop-$RUN_ID.dump"
```

- 归档格式:`pg_dump -Fc`;默认拒绝覆盖同名 `.dump` 或 `.json`。
- 工具成功、`pg_restore -l` 可读、文件大小与 SHA-256 校验、归档/元信息落盘全部成功后才返回成功。
- 元信息 `<name>.json` 含:格式版本、run_id、时间、PostgreSQL/pg_dump/pg_restore 版本、
  Alembic 版本、pgvector 扩展版本、数据库非敏感标识、大小、SHA-256、代码 HEAD/状态哈希/
  工作区内容指纹、计数/状态/业务摘要(不含业务全文或口令哈希)。
- 输出目录不可写、`pg_dump` 非零、归档为空/不可读、元信息写入失败均非零退出。
- 取消(可捕获的 `KeyboardInterrupt`)返回 130,并清理本次临时文件;不会留下“成功备份”。
- 容器内临时文件只清理本次生成的 `/tmp/opssop-*` 确切路径;清理失败时归档仍保留,
  但结果标记 `core_success=true`、`cleanup_complete=false`、`status=cleanup_incomplete`,
  CLI 以非零(4)退出,不把只有 warning 的情况当作完整成功。

**归档包含**:业务 schema、数据、序列、Alembic 版本、pgvector 扩展/索引数据。
**不包含**:PostgreSQL 集群级角色/密码、`pg_hba.conf`、`postgresql.conf`、外部模型配置;
真实模型/embedding 密钥不属于数据库逻辑备份,由部署方单独保存和轮换。

## 2. 恢复到全新库

```powershell
$run = "restore20261022a"
python backend/scripts/ops_cli.py restore `
  --project opssop-demo-p4b1 `
  --env-file .env.demo `
  --compose-file docker-compose.yml `
  --run-id $run `
  --archive "D:\opssop-backups\opssop-backup20261022a.dump" `
  --metadata "D:\opssop-backups\opssop-backup20261022a.json" `
  --target-db "opssop_restore_$run" `
  --out-dir "D:\opssop-restore"
```

```bash
RUN_ID=restore20261022a
python3 backend/scripts/ops_cli.py restore \
  --project opssop-demo-p4b1 \
  --env-file .env.demo \
  --compose-file docker-compose.yml \
  --run-id "$RUN_ID" \
  --archive "/var/backups/opssop/opssop-backup20261022a.dump" \
  --metadata "/var/backups/opssop/opssop-backup20261022a.json" \
  --target-db "opssop_restore_$RUN_ID" \
  --out-dir /var/backups/opssop/restore
```

### 安全顺序

1. 校验元信息 schema/格式、归档存在、大小、SHA-256、`pg_restore -l` 可读性;
2. 在创建目标库前校验恢复所需元信息完整结构:来源库标识、`source_snapshot` 的
   表计数/状态分布/业务摘要/Alembic/pgvector 版本及表清单;缺失或畸形即拒绝;
3. 校验目标名安全、目标不存在、目标 != 来源库、来源/目标 PostgreSQL 主版本一致;
4. 才创建目标库;
5. `pg_restore --no-owner --no-privileges --exit-on-error`;
6. 快照比对计数/状态/业务摘要/Alembic/pgvector 版本;不一致即失败。

### 目标保护

- 目标必须显式指定,且只能是新库;已存在(包括空库)一律拒绝。
- 拒绝 `postgres`、`template0/1`、`opssop`、`opssop_test`、`opssop_demo`、`opssop_prod`
  及测试/评测命名空间。
- 拒绝目标与元信息来源库相同。
- 不提供 `--clean`、自动 drop/truncate/覆盖开关。
- 系统/模板/来源库和受保护目标不会被本工具修改。

### 失败与取消

- `pg_restore` 失败/校验不一致/取消:非零(取消 130);结果 JSON 记录各阶段。
- 默认保留本轮已创建但不可用的目标库,便于诊断;`--cleanup-failed-target` 才按本轮
  创建记录清理该目标。绝不处理启动前已存在的库。
- 只清理归档的容器内临时路径;不会删除其他库或卷。临时文件清理失败时恢复数据可保留,
  但 `success=false`、`status=cleanup_incomplete`,CLI 非零退出。

### 角色/权限

逻辑备份不恢复 PostgreSQL 集群角色。应用数据库用户和 PostgreSQL 登录角色不是同一概念:

- 预先准备目标集群的登录角色、数据库连接权限和必要 schema 权限;
- `--no-owner --no-privileges` 避免归档中的 OWNER/GRANT 指向不存在角色;
- 业务表属主/授权需按目标环境另行核对;本工具不承诺自动恢复集群全部角色。

## 3. 恢复后核对与续办

```powershell
# 在 DB 容器内核对迁移版本/关键计数(示例;用户名/库名按实际替换)
docker compose -p opssop-demo-p4b1 --env-file .env.demo exec -T db `
  psql -X -U opssop -d opssop_restore_restore20261022a -Atc `
  "SELECT version_num FROM alembic_version; SELECT count(*) FROM sops;"
```

- 用指向恢复库的另一套后端/前端实例实际 HTTP 登录、检索和权限检查;
  不能只用 TestClient/SQL 代替。
- 正式检索只应返回已审核案例/有效发布 SOP;草稿、待审/退回、无权材料不可见。
- 通过业务 API 新建故障/补证/确认/审核,核对新 ID 和审计,证明序列可续分配。
- 若 embedding 配置与备份索引不兼容,保留“需重建/关键词降级”规则,人工执行:
  `python backend/scripts/build_embedding_index.py`(按部署配置);不得混用旧向量。

## 4. 升级与回退

1. 升级前先备份,并记录备份 SHA-256/元信息;
2. 在**新库**恢复/验证备份,再对目标走受支持的 Alembic 升级路径;
3. 升级失败时切回旧应用镜像与旧库/旧备份;不要把“回滚镜像”当成“回滚数据库”;
4. 不承诺数据库降级一定无损;需要降级时先在副本演练并人工确认。

## 5. 私有数据与保留

- 归档含账号哈希与业务内容,属于私有数据;不要提交 Git、公开对象存储或聊天记录。
- 操作者应离机保存、限制访问、定期恢复验证;本批不实现自动异地容灾/保留策略执行器。
- 备份成功不等于恢复成功;至少按本批演练方式定期做全新目标库恢复和业务续办验证。

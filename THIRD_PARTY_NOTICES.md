# 第三方依赖说明

OpsSOP 的 MIT 许可证覆盖本项目原创代码。第三方依赖、开发工具及 Docker 基础镜像保留各自的版权和许可证，不因本项目采用 MIT 而改变。

本源码仓库不包含 `node_modules`、Python 已安装依赖、镜像或数据库内容。安装或分发依赖时，应保留对应软件包提供的版权、许可证与 NOTICE 文件。

依赖清单及版本以以下文件为准：

- 前端直接依赖：`frontend/package.json`
- 前端完整依赖版本：`frontend/package-lock.json`
- 后端依赖版本：`backend/requirements.txt`
- 基础镜像：前后端 `Dockerfile` 与 `docker-compose.yml`

主要组件包括 Vue、TypeScript、Vite、Element Plus、Tailwind CSS、Axios、FastAPI、SQLAlchemy、Alembic、PostgreSQL、Node.js、Python 和 nginx。各组件的许可证请查看其官方发行包与仓库；前端锁文件也记录依赖声明的许可证信息。

部分构建工具还使用独立许可的数据集，例如 `caniuse-lite`。这些依赖的许可要求应按其发行版本核对。

第二阶段新增测试工具的锁文件声明（非穷尽清单，2026-10-14）：

| 软件包 | 锁定版本 | `license` 字段 |
|---|---|---|
| Vitest (`vitest`) | 2.1.9 | MIT |
| happy-dom | 15.11.7 | MIT |
| Vue Test Utils (`@vue/test-utils`) | 2.5.1 | MIT |

以上依据 `frontend/package-lock.json` 的对应条目核对，不替代完整依赖审计；再分发构建产物或依赖时仍应保留各发行包要求的声明。

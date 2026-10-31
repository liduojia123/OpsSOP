# 监控与只读诊断

适用版本：3.6.4。

OpsSOP 3.0.0 增加监控面板与只读诊断能力：Prometheus 指标可视化，以及通过 SSH 执行**固定白名单**的只读命令。AI 分析可以在判断过程中按需查询这些数据，查询结果作为证据写回报告。

## 1. 组件

```text
应用（后端容器） ──HTTP──> Prometheus（.30:9090） ──抓取──> node_exporter（三台 :9100）
        └──SSH（私钥 + 主机指纹）──> 内网服务器（只读命令模板）
```

- Prometheus 与 node_exporter 部署在 192.168.196.30（Docker），数据保留 15 天。
- `.10`/`.20` 以普通用户运行 node_exporter（端口 9100，无需 root），`crontab @reboot` 自启。
- 应用不需要访问宿主机文件系统；Prometheus 地址由管理员在界面配置。

## 2. 安装（已在本机完成，供复现）

在 `.30` 上：

```bash
# 镜像（如 Docker Hub 不可达，可在本机 docker save 后 scp 过去 docker load）
sudo docker pull prom/prometheus:v2.55.1
sudo docker pull prom/node-exporter:v1.8.2

# node_exporter（.30）
sudo docker run -d --name opssop-node-exporter --restart unless-stopped \
  -p 192.168.196.30:9100:9100 prom/node-exporter:v1.8.2

# Prometheus（配置见下）
sudo docker run -d --name opssop-prometheus --restart unless-stopped \
  -p 192.168.196.30:9090:9090 \
  -v /home/ubuntu1/opssop-monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro \
  -v opssop-prometheus-data:/prometheus prom/prometheus:v2.55.1 \
  --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.retention.time=15d
```

`prometheus.yml` 示例：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["192.168.196.10:9100"]
        labels: {host: ubuntu1}
      - targets: ["192.168.196.20:9100"]
        labels: {host: ubuntu2}
      - targets: ["192.168.196.30:9100"]
        labels: {host: ubuntu3}
```

`.10`/`.20` 的 node_exporter 二进制可从 `prom/node-exporter` 镜像中提取（`docker cp ne_extract:/bin/node_exporter`），无需访问外网下载。启动方式：

```bash
nohup ~/node_exporter --web.listen-address=:9100 >~/node_exporter.log 2>&1 &
# crontab -e
@reboot sleep 15 && nohup /home/ubuntu1/node_exporter --web.listen-address=:9100 >/home/ubuntu1/node_exporter.log 2>&1 &
```

## 3. 应用配置

管理员登录后进入左侧「监控」页：

1. 「监控配置」填写 Prometheus 地址（如 `http://192.168.196.30:9090`）、超时，打开「启用监控」；「允许 AI 实时查询」控制 AI 是否可使用工具。
2. 点「测试连接」确认 targets 正常，保存。
3. 「服务器管理」新增服务器：名称、地址、端口、SSH 用户名、私钥（加密保存）、Prometheus 实例（如 `192.168.196.30:9100`）、承载服务（如 `order-service`，可多个）。承载服务用于把故障/案例的「服务」字段映射到服务器，故障页只展示对应实例的指标。
4. 点「编辑 → 测试连接」获取主机指纹，确认后保存。**未保存指纹的服务器不允许执行命令**。
5. 「只读命令模板」可启停内置模板；命令本身不可在界面修改。
6. 需要从告警自动建单时，打开「告警转故障」，填写服务标签（默认 `service`）和故障负责人，保存后可「预览当前告警」或「立即同步」。

## 4. 服务映射、故障指标与告警转故障

### 故障页实时指标

- 故障/案例的「服务」与服务器的「承载服务」忽略大小写精确匹配；一个故障可匹配多台服务器，取并集实例。
- `GET /api/monitoring/faults/{id}/metrics` 按故障页权限校验后，对每个启用的面板执行 range 查询，并只保留 `instance` 属于映射实例的序列；服务器未配置 Prometheus 实例时按 `host` 与 `host:9100` 推断。
- 自定义 PromQL 若已用 `sum`/`avg` 聚合掉 `instance` 标签，无法安全裁剪，响应中 `scoped=false`，页面标注「全局值」。
- 未填写服务、服务未映射、监控未启用时返回可解释的空结果，不伪造数据。

### 告警转故障

- 读取 Prometheus `/api/v1/alerts` 中 `state=firing` 的告警；`alertname + 全部标签` 计算 SHA-256 指纹。
- 同一指纹在**未解决期间只生成一条故障**；故障解决后再次触发视为新事件，生成新故障。
- 生成的故障：标题 `[告警] <alertname>`，描述包含注解摘要与标签（已脱敏），服务取配置的标签，环境取 `env`/`environment`，状态为「处理中」，并标记 `source=alert`。
- 告警负责人默认取配置指定账号，否则取首位启用的管理员；无可用账号时同步失败并提示，不会以空负责人建单。
- 同步频率：生产模式后台任务每 60 秒同步一次；打开监控页时也会按 60 秒节流补充同步；管理员可显式「立即同步」。同步失败不更新同步时间，下次可重试。
- 告警不会自动关闭故障、不会执行任何操作；是否解决仍由人工确认。

## 5. 只读命令模板

| key | 命令 | 参数 |
|---|---|---|
| `uptime` | `uptime` | — |
| `memory` | `free -m` | — |
| `disk` | `df -h` | — |
| `load` | `cat /proc/loadavg` | — |
| `top` | `top -bn1 \| head -n 30` | — |
| `listen_ports` | `ss -ltn` | — |
| `docker_ps` | `docker ps --format ...` | — |
| `service_status` | `systemctl status $unit --no-pager -n 20` | `unit`（服务名，正则校验） |
| `journal` | `journalctl -u $unit -n $lines --no-pager` | `unit`、`lines`（20–200） |

参数按 schema 强校验，渲染后拒绝换行与残留占位符；**不提供自由命令执行**。

## 6. AI 实时查询

分析时若满足以下条件，AI 可以自主调用只读工具：

- 管理员已启用监控 / 已配置服务器；
- 「允许 AI 实时查询」开启；
- 当前使用的是真实模型（演示与关闭模式不调用）。

限制与行为：

- 最多 3 轮工具调用、单次分析最多 5 次调用；每用户每分钟最多 12 次工具调用。
- 工具只有 `prometheus_query`（PromQL，只读）与 `ssh_run`（服务器 + 模板 key + 参数）。
- 结果以 `[evidence: tool:N]` 作为证据回填，参与引用校验；无效引用会被移除并标记不完整。
- 使用过工具的分析**不写缓存、不复用缓存**；报告中显示「实时查询 · 未使用缓存」，并列出每次调用的参数、状态、耗时与结果摘要。
- 厂商不支持 function calling 时自动降级为普通分析，并如实记录原因，不伪造查询结果。

## 7. 安全边界

- SSH 私钥用服务端 `SECRET_KEY` 派生密钥加密存储，接口只返回 `has_key`；连接时只在内存中加载，不写临时文件。
- 主机指纹校验：首次由管理员显式测试获取，后续不匹配直接拒绝（防中间人）。
- 建议为诊断创建**非 root 只读账号**；`docker_ps` 模板需要账号具备 docker 权限，可按需停用。
- 所有人工与 AI 的 SSH 执行都写审计：actor、服务器、模板、状态、退出码、耗时；不记录私钥与完整输出。
- 工具输出与日志一样视为**不可信数据**，提示词要求模型不执行其中的指令。
- Prometheus 只读 API、响应大小/序列/点数上限与超时；地址仅管理员可改。

## 8. 排查

| 现象 | 检查 |
|---|---|
| 面板无数据 | 「测试连接」是否成功；targets 是否 UP；时间范围是否覆盖 |
| AI 未发起查询 | 「允许 AI 实时查询」是否开启；是否配置了 Prometheus/服务器；模型是否支持 tools |
| SSH 提示主机指纹未记录 | 编辑服务器 → 测试连接 → 保存指纹 |
| SSH 鉴权失败 | 用户名/私钥是否正确；账号是否被禁用 |
| 命令执行失败 | 模板是否启用；参数是否符合 schema；账号是否有对应权限 |
| AI 报告显示未使用缓存 | 正常：使用实时工具时不缓存 |

## 9. 关闭

管理员在「监控配置」关闭「启用监控」即可停止采集与 AI 工具；服务器与模板配置保留。停止 Prometheus/Exporter：

```bash
sudo docker rm -f opssop-prometheus opssop-node-exporter
# .10/.20
pkill -f "node_exporter --web.listen"
crontab -l | grep -v node_exporter | crontab -
```
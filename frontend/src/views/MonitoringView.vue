<script setup lang="ts">
/**
 * 监控面板:Prometheus 指标、抓取目标、服务器与人工只读诊断命令。
 * 管理员可在此配置数据源、服务器与命令模板启停。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import MetricChart from '../components/MetricChart.vue'
import { fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
import type {
  AlertPreview,
  AlertSyncResult,
  MonitoredServer,
  MonitoringConfig,
  MonitoringOverview,
  MonitoringPanel,
  RunCommandResult,
  ServerTestResult,
  SshTemplate,
  UserOption,
} from '../types'

const auth = useAuthStore()
const isAdmin = computed(() => auth.role === 'admin')

const overview = ref<MonitoringOverview | null>(null)
const loading = ref(false)
const loadError = ref('')
const rangeMinutes = ref(60)
const autoRefresh = ref(true)
let timer: number | undefined

const servers = ref<MonitoredServer[]>([])
const templates = ref<SshTemplate[]>([])
const drawerOpen = ref(false)
const runServerId = ref<number | null>(null)
const runTemplate = ref('')
const runArgs = reactive<Record<string, number | string>>({})
const running = ref(false)
const cmdResult = ref<RunCommandResult | null>(null)
const cmdError = ref('')

const config = ref<MonitoringConfig | null>(null)
const configForm = reactive({
  enabled: false,
  prometheus_url: '',
  timeout_seconds: 10,
  ai_tools_enabled: true,
  alerts_enabled: false,
  alert_service_label: 'service',
  alert_owner_id: null as number | null,
})
const userOptions = ref<UserOption[]>([])
const alertPreview = ref<AlertPreview | null>(null)
const alertChecking = ref(false)
const alertSyncing = ref(false)
const savingConfig = ref(false)
const testingProm = ref(false)
const promTest = ref('')
const adminServers = ref<MonitoredServer[]>([])
const adminTemplates = ref<SshTemplate[]>([])
const serverDialog = ref(false)
const serverSaving = ref(false)
const serverTesting = ref(false)
const serverTest = ref<ServerTestResult | null>(null)
const editingId = ref<number | null>(null)
const serverForm = reactive({
  name: '',
  host: '',
  port: 22,
  username: '',
  private_key: '',
  host_key_fingerprint: '',
  prometheus_instance: '',
  service_names: [] as string[],
  enabled: true,
  notes: '',
})

const upCount = computed(
  () => overview.value?.targets.filter((t) => t.health === 'up').length ?? 0,
)
const targetCount = computed(() => overview.value?.targets.length ?? 0)
const selectedTemplate = computed(
  () => templates.value.find((t) => t.key === runTemplate.value) || null,
)

function panelLatest(panel: MonitoringPanel): string {
  const points = panel.series.flatMap((s) => s.points)
  const last = points.length ? points[points.length - 1][1] : undefined
  if (last === undefined || !Number.isFinite(Number(last))) return '—'
  const value = Number(last)
  if (panel.unit === 'percent') return `${value.toFixed(1)}%`
  if (panel.unit === 'bytes') {
    if (Math.abs(value) > 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB/s`
    if (Math.abs(value) > 1024) return `${(value / 1024).toFixed(1)} KB/s`
    return `${value.toFixed(0)} B/s`
  }
  return value.toFixed(2)
}

async function loadOverview() {
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await client.get<MonitoringOverview>('/api/monitoring/overview', {
      params: { minutes: rangeMinutes.value, step: rangeMinutes.value >= 360 ? 300 : 60 },
    })
    overview.value = data
  } catch (e) {
    loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

async function loadDiagnostics() {
  try {
    const [serverResp, templateResp] = await Promise.all([
      client.get<MonitoredServer[]>('/api/monitoring/servers'),
      client.get<SshTemplate[]>('/api/monitoring/templates'),
    ])
    servers.value = serverResp.data
    templates.value = templateResp.data
  } catch {
    /* 诊断入口失败不阻塞指标展示 */
  }
}

async function loadAdmin() {
  if (!isAdmin.value) return
  try {
    const [configResp, serverResp, templateResp, userResp] = await Promise.all([
      client.get<MonitoringConfig>('/api/admin/monitoring'),
      client.get<MonitoredServer[]>('/api/admin/servers'),
      client.get<SshTemplate[]>('/api/admin/ssh-templates'),
      client.get<UserOption[]>('/api/users/options'),
    ])
    config.value = configResp.data
    Object.assign(configForm, {
      enabled: configResp.data.enabled,
      prometheus_url: configResp.data.prometheus_url,
      timeout_seconds: configResp.data.timeout_seconds,
      ai_tools_enabled: configResp.data.ai_tools_enabled,
      alerts_enabled: configResp.data.alerts_enabled,
      alert_service_label: configResp.data.alert_service_label,
      alert_owner_id: configResp.data.alert_owner_id,
    })
    adminServers.value = serverResp.data
    adminTemplates.value = templateResp.data
    userOptions.value = userResp.data
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

watch(rangeMinutes, () => void loadOverview())
watch(runTemplate, () => {
  cmdResult.value = null
  cmdError.value = ''
  const template = selectedTemplate.value
  for (const key of Object.keys(runArgs)) delete runArgs[key]
  if (!template) return
  for (const [key, rule] of Object.entries(template.param_schema || {})) {
    runArgs[key] = rule.default ?? (rule.type === 'int' ? 100 : '')
  }
})

function openDrawer(server?: MonitoredServer) {
  cmdResult.value = null
  cmdError.value = ''
  runServerId.value = server?.id ?? servers.value[0]?.id ?? null
  if (!runTemplate.value) runTemplate.value = templates.value[0]?.key ?? ''
  drawerOpen.value = true
}

async function runCommand() {
  if (!runServerId.value) {
    cmdError.value = '请选择服务器'
    return
  }
  if (!runTemplate.value) {
    cmdError.value = '请选择命令模板'
    return
  }
  running.value = true
  cmdError.value = ''
  cmdResult.value = null
  try {
    const args: Record<string, number | string> = {}
    for (const [key, value] of Object.entries(runArgs)) {
      if (value !== '' && value !== undefined) args[key] = value
    }
    const { data } = await client.post<RunCommandResult>(
      `/api/monitoring/servers/${runServerId.value}/run`,
      { template: runTemplate.value, args },
      { timeout: 60000 },
    )
    cmdResult.value = data
  } catch (e) {
    cmdError.value = errMsg(e)
  } finally {
    running.value = false
  }
}

async function saveConfig() {
  savingConfig.value = true
  try {
    const { data } = await client.put<MonitoringConfig>('/api/admin/monitoring', {
      enabled: configForm.enabled,
      prometheus_url: configForm.prometheus_url,
      timeout_seconds: configForm.timeout_seconds,
      ai_tools_enabled: configForm.ai_tools_enabled,
      alerts_enabled: configForm.alerts_enabled,
      alert_service_label: configForm.alert_service_label,
      alert_owner_id: configForm.alert_owner_id,
    })
    config.value = data
    ElMessage.success('监控配置已保存')
    await loadOverview()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    savingConfig.value = false
  }
}

async function testPrometheus() {
  testingProm.value = true
  promTest.value = ''
  try {
    const { data } = await client.post('/api/admin/monitoring/test', {
      prometheus_url: configForm.prometheus_url,
    })
    promTest.value = data.message || (data.ok ? '连接成功' : '连接失败')
  } catch (e) {
    promTest.value = errMsg(e)
  } finally {
    testingProm.value = false
  }
}

async function checkAlerts() {
  alertChecking.value = true
  try {
    const { data } = await client.get<AlertPreview>('/api/admin/monitoring/alerts')
    alertPreview.value = data
    if (!data.ok && data.error) ElMessage.warning(data.error)
  } catch (e) {
    alertPreview.value = null
    ElMessage.error(errMsg(e))
  } finally {
    alertChecking.value = false
  }
}

async function syncAlertsNow() {
  alertSyncing.value = true
  try {
    const { data } = await client.post<AlertSyncResult>('/api/admin/monitoring/alerts/sync')
    if (!data.ok) {
      ElMessage.warning(data.error || '同步未完成')
    } else {
      ElMessage.success(
        data.created.length
          ? `已从告警生成 ${data.created.length} 条故障`
          : `没有新增故障（当前告警 ${data.firing} 条，已去重）`,
      )
    }
    await Promise.all([loadAdmin(), loadOverview()])
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    alertSyncing.value = false
  }
}

function openServerDialog(row?: MonitoredServer) {
  editingId.value = row?.id ?? null
  serverTest.value = null
  Object.assign(serverForm, {
    name: row?.name ?? '',
    host: row?.host ?? '',
    port: row?.port ?? 22,
    username: row?.username ?? '',
    private_key: '',
    host_key_fingerprint: row?.host_key_fingerprint ?? '',
    prometheus_instance: row?.prometheus_instance ?? '',
    service_names: [...(row?.service_names ?? [])],
    enabled: row?.enabled ?? true,
    notes: row?.notes ?? '',
  })
  serverDialog.value = true
}

async function saveServer() {
  serverSaving.value = true
  try {
    const payload = {
      name: serverForm.name,
      host: serverForm.host,
      port: serverForm.port,
      username: serverForm.username,
      private_key: serverForm.private_key,
      host_key_fingerprint: serverForm.host_key_fingerprint,
      prometheus_instance: serverForm.prometheus_instance,
      service_names: [...serverForm.service_names],
      enabled: serverForm.enabled,
      notes: serverForm.notes,
    }
    if (editingId.value) {
      await client.patch(`/api/admin/servers/${editingId.value}`, payload)
    } else {
      await client.post('/api/admin/servers', payload)
    }
    ElMessage.success('服务器已保存')
    serverDialog.value = false
    await Promise.all([loadAdmin(), loadDiagnostics()])
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    serverSaving.value = false
  }
}

async function testServer() {
  if (!editingId.value) {
    ElMessage.warning('请先保存服务器，再测试连接')
    return
  }
  serverTesting.value = true
  serverTest.value = null
  try {
    const { data } = await client.post<ServerTestResult>(
      `/api/admin/servers/${editingId.value}/test`,
      { private_key: serverForm.private_key },
      { timeout: 30000 },
    )
    serverTest.value = data
    if (data.ok && data.fingerprint) {
      serverForm.host_key_fingerprint = data.fingerprint
    }
  } catch (e) {
    serverTest.value = { ok: false, fingerprint: '', message: errMsg(e) }
  } finally {
    serverTesting.value = false
  }
}

async function toggleTemplate(row: SshTemplate) {
  try {
    await client.patch(`/api/admin/ssh-templates/${row.id}`, null, {
      params: { enabled: !row.enabled },
    })
    await loadAdmin()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function removeServer(row: MonitoredServer) {
  try {
    await ElMessageBox.confirm(`确认删除服务器「${row.name}」?`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await client.delete(`/api/admin/servers/${row.id}`)
    ElMessage.success('已删除')
    await Promise.all([loadAdmin(), loadDiagnostics()])
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(async () => {
  await Promise.all([loadOverview(), loadDiagnostics()])
  if (isAdmin.value) await loadAdmin()
  timer = window.setInterval(() => {
    if (autoRefresh.value) void loadOverview()
  }, 30000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="ops-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">OPERATIONS / TELEMETRY</div>
        <h1>监控</h1>
        <p class="ops-page-description">
          查看服务器实时指标，必要时执行只读诊断命令；AI 分析也可按需查询这些数据。
        </p>
      </div>
      <div class="ops-page-actions">
        <span class="ops-chip" :class="{ 'is-warn': upCount < targetCount }">
          <AppIcon name="activity" :size="15" />{{ upCount }}/{{ targetCount }} 目标正常
        </span>
        <el-select v-model="rangeMinutes" class="mon-range" size="small">
          <el-option :value="15" label="近 15 分钟" />
          <el-option :value="60" label="近 1 小时" />
          <el-option :value="360" label="近 6 小时" />
          <el-option :value="1440" label="近 24 小时" />
        </el-select>
        <label class="mon-toggle"><span>自动刷新</span><el-switch v-model="autoRefresh" /></label>
        <el-button :loading="loading" @click="loadOverview"><AppIcon name="refresh" :size="15" />刷新</el-button>
      </div>
    </header>

    <el-alert v-if="loadError" class="mb-5" :title="loadError" type="error" :closable="false" show-icon />
    <el-alert
      v-if="overview && !overview.enabled"
      class="mb-5"
      title="监控尚未启用"
      :description="isAdmin ? '请在页面底部「监控配置」中填写 Prometheus 地址并启用。' : '请联系管理员配置 Prometheus 数据源。'"
      type="info"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="overview?.error"
      class="mb-5"
      :title="overview.error"
      type="warning"
      :closable="false"
      show-icon
    />

    <div v-if="overview?.panels.length" class="mon-grid">
      <section v-for="panel in overview.panels" :key="panel.id" class="ops-panel mon-panel">
        <div class="mon-panel-head">
          <div>
            <h3>{{ panel.title }}</h3>
            <p class="mon-panel-value">{{ panelLatest(panel) }}</p>
          </div>
          <span v-if="panel.truncated" class="ops-section-caption">已截断</span>
        </div>
        <div class="mon-panel-body">
          <MetricChart v-if="panel.series.length && !panel.error" :series="panel.series" :unit="panel.unit" />
          <div v-else class="ops-empty is-quiet">
            <AppIcon name="activity" :size="20" />
            <div>
              <strong>{{ panel.error ? '查询失败' : '暂无数据' }}</strong>
              <span>{{ panel.error || '该指标在当前时间范围没有数据' }}</span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <section class="ops-panel ops-table-panel mb-6">
      <div class="ops-section-header">
        <div>
          <h2><AppIcon name="activity" :size="17" />抓取目标</h2>
          <p>Prometheus 当前 targets 与最近抓取状态。</p>
        </div>
        <span class="ops-section-caption">更新于 {{ overview ? fmtTime(overview.fetched_at) : '—' }}</span>
      </div>
      <el-table :data="overview?.targets ?? []" empty-text="暂无抓取目标">
        <el-table-column label="任务" prop="job" width="120" />
        <el-table-column label="实例" min-width="200">
          <template #default="{ row }"><span class="ops-id">{{ row.instance }}</span></template>
        </el-table-column>
        <el-table-column label="主机" width="130">
          <template #default="{ row }">{{ row.host || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.health === 'up' ? 'success' : 'danger'">
              {{ row.health || 'unknown' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="抓取耗时" width="120">
          <template #default="{ row }">
            {{ row.scrape_duration ? `${(Number(row.scrape_duration) * 1000).toFixed(0)} ms` : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="最近抓取" width="190">
          <template #default="{ row }">{{ row.last_scrape ? fmtTime(row.last_scrape) : '—' }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="ops-panel ops-table-panel mb-6">
      <div class="ops-section-header">
        <div>
          <h2><AppIcon name="server" :size="17" />诊断服务器</h2>
          <p>只读命令在服务器上执行，参数由服务端强校验；每次执行都会写审计。</p>
        </div>
      </div>
      <el-table :data="servers" empty-text="暂无已配置服务器">
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }"><span class="mon-server-name">{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column label="地址" min-width="200">
          <template #default="{ row }"><span class="ops-id">{{ row.username }}@{{ row.host }}:{{ row.port }}</span></template>
        </el-table-column>
        <el-table-column label="指标实例" min-width="170">
          <template #default="{ row }">{{ row.prometheus_instance || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="right">
          <template #default="{ row }">
            <el-button size="small" plain @click="openDrawer(row)">诊断</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <template v-if="isAdmin">
      <section class="ops-panel mb-6">
        <div class="ops-section-header">
          <div>
            <h2><AppIcon name="settings" :size="17" />监控配置</h2>
            <p>管理员在这里配置 Prometheus；保存后立即生效，AI 实时查询也依赖此配置。</p>
          </div>
          <span v-if="config?.updated_at" class="ops-section-caption">最近保存 {{ fmtTime(config.updated_at) }}</span>
        </div>
        <div class="mon-config-body">
          <div class="mon-config-grid">
            <div>
              <label class="form-label">Prometheus 地址</label>
              <el-input v-model="configForm.prometheus_url" placeholder="http://192.168.196.30:9090" />
            </div>
            <div>
              <label class="form-label">请求超时（秒）</label>
              <el-input-number v-model="configForm.timeout_seconds" :min="3" :max="30" />
            </div>
            <div class="mon-config-switch">
              <label class="form-label">启用监控</label>
              <el-switch v-model="configForm.enabled" />
            </div>
            <div class="mon-config-switch">
              <label class="form-label">允许 AI 实时查询</label>
              <el-switch v-model="configForm.ai_tools_enabled" />
            </div>
            <div class="mon-config-switch">
              <label class="form-label">告警转故障</label>
              <el-switch v-model="configForm.alerts_enabled" />
            </div>
            <div>
              <label class="form-label">告警服务标签</label>
              <el-input v-model="configForm.alert_service_label" placeholder="service" />
            </div>
            <div>
              <label class="form-label">告警故障负责人</label>
              <el-select v-model="configForm.alert_owner_id" clearable placeholder="默认首位管理员" class="w-full">
                <el-option
                  v-for="u in userOptions"
                  :key="u.id"
                  :label="`${u.display_name}（${u.username}）`"
                  :value="u.id"
                />
              </el-select>
            </div>
          </div>
          <div class="mon-config-actions">
            <el-button :loading="testingProm" @click="testPrometheus">测试连接</el-button>
            <el-button type="primary" :loading="savingConfig" @click="saveConfig">保存配置</el-button>
            <span v-if="promTest" class="ops-section-caption">{{ promTest }}</span>
          </div>
        </div>
      </section>

      <section v-if="configForm.alerts_enabled" class="ops-panel mb-6">
        <div class="ops-section-header">
          <div>
            <h2><AppIcon name="alert" :size="17" />告警同步</h2>
            <p>读取 Prometheus 告警并按标签指纹去重生成故障；不会自动关闭或执行任何操作。</p>
          </div>
          <div class="mon-config-actions">
            <span v-if="config?.alert_last_sync_at" class="ops-section-caption">
              上次同步 {{ fmtTime(config.alert_last_sync_at) }}
            </span>
            <el-button :loading="alertChecking" @click="checkAlerts">预览当前告警</el-button>
            <el-button type="primary" plain :loading="alertSyncing" @click="syncAlertsNow">立即同步</el-button>
          </div>
        </div>
        <div v-if="alertPreview?.error" class="ops-section-caption mb-3">{{ alertPreview.error }}</div>
        <el-table :data="alertPreview?.items ?? []" empty-text="点击「预览当前告警」读取当前规则告警" size="small">
          <el-table-column label="告警" min-width="160">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.state === 'firing' ? 'danger' : 'info'">{{ row.state }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="严重度" width="90">
            <template #default="{ row }">{{ row.severity || '-' }}</template>
          </el-table-column>
          <el-table-column label="实例" min-width="150">
            <template #default="{ row }"><span class="ops-id">{{ row.instance || '-' }}</span></template>
          </el-table-column>
          <el-table-column label="服务" width="110">
            <template #default="{ row }">{{ row.service || '-' }}</template>
          </el-table-column>
          <el-table-column label="摘要" min-width="220">
            <template #default="{ row }">{{ row.summary || '-' }}</template>
          </el-table-column>
        </el-table>
      </section>

      <section class="ops-panel ops-table-panel mb-6">
        <div class="ops-section-header">
          <div>
            <h2><AppIcon name="server" :size="17" />服务器管理</h2>
            <p>私钥加密保存，页面不回显；主机指纹用于防止中间人攻击。</p>
          </div>
          <el-button type="primary" plain @click="openServerDialog()">新增服务器</el-button>
        </div>
        <el-table :data="adminServers" empty-text="暂无服务器">
          <el-table-column label="名称" min-width="130">
            <template #default="{ row }">{{ row.name }}</template>
          </el-table-column>
          <el-table-column label="地址" min-width="200">
            <template #default="{ row }"><span class="ops-id">{{ row.username }}@{{ row.host }}:{{ row.port }}</span></template>
          </el-table-column>
          <el-table-column label="承载服务" min-width="160">
            <template #default="{ row }">
              <span v-if="row.service_names?.length" class="ops-text-soft">{{ row.service_names.join('、') }}</span>
              <span v-else class="ops-text-faint">未映射</span>
            </template>
          </el-table-column>
          <el-table-column label="指纹" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.host_key_fingerprint ? 'success' : 'warning'">
                {{ row.host_key_fingerprint ? '已确认' : '未确认' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '否' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" align="right">
            <template #default="{ row }">
              <el-button size="small" plain @click="openServerDialog(row)">编辑</el-button>
              <el-button size="small" type="danger" plain @click="removeServer(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section class="ops-panel ops-table-panel">
        <div class="ops-section-header">
          <div>
            <h2><AppIcon name="command" :size="17" />只读命令模板</h2>
            <p>内置模板命令不可修改，只能启停；AI 与人工诊断都只能使用启用的模板。</p>
          </div>
        </div>
        <el-table :data="adminTemplates" empty-text="暂无模板">
          <el-table-column label="模板" min-width="160">
            <template #default="{ row }">
              <div class="mon-template-name">{{ row.name }}<span class="ops-id">{{ row.key }}</span></div>
            </template>
          </el-table-column>
          <el-table-column label="命令" min-width="280">
            <template #default="{ row }"><code class="mon-command">{{ row.command }}</code></template>
          </el-table-column>
          <el-table-column label="启用" width="90">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" @change="toggleTemplate(row)" />
            </template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <el-drawer v-model="drawerOpen" title="只读诊断" size="520px">
      <div class="mon-drawer">
        <label class="form-label">服务器</label>
        <el-select v-model="runServerId" class="w-full">
          <el-option v-for="s in servers" :key="s.id" :label="`${s.name}（${s.host}）`" :value="s.id" />
        </el-select>
        <label class="form-label mt-3">命令模板</label>
        <el-select v-model="runTemplate" class="w-full">
          <el-option v-for="t in templates" :key="t.key" :label="t.name" :value="t.key" />
        </el-select>
        <template v-if="selectedTemplate">
          <div v-for="[key, rule] in Object.entries(selectedTemplate.param_schema || {})" :key="key">
            <label class="form-label mt-3">{{ key }}</label>
            <el-input-number
              v-if="rule.type === 'int'"
              :model-value="Number(runArgs[key] ?? 100)"
              :min="rule.min"
              :max="rule.max"
              class="w-full"
              @update:model-value="(v: number | undefined) => (runArgs[key] = v ?? 0)"
            />
            <el-input
              v-else
              :model-value="String(runArgs[key] ?? '')"
              :placeholder="String(rule.pattern || '')"
              @update:model-value="(v: string) => (runArgs[key] = v)"
            />
          </div>
        </template>
        <div class="mon-drawer-actions">
          <el-button type="primary" :loading="running" @click="runCommand">执行</el-button>
        </div>
        <el-alert v-if="cmdError" :title="cmdError" type="error" :closable="false" show-icon class="mt-3" />
        <template v-if="cmdResult">
          <div class="mon-cmd-meta">
            <span>{{ cmdResult.server }} · {{ cmdResult.template }}</span>
            <span>退出码 {{ cmdResult.exit_status }} · {{ cmdResult.latency_ms }} ms</span>
          </div>
          <pre class="mon-output">{{ cmdResult.stdout || '(无输出)' }}</pre>
          <p v-if="cmdResult.truncated" class="ops-section-caption">输出已截断</p>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="serverDialog" :title="editingId ? '编辑服务器' : '新增服务器'" width="560px" append-to-body>
      <div class="mon-server-form">
        <div>
          <label class="form-label">名称</label>
          <el-input v-model="serverForm.name" placeholder="ubuntu3" />
        </div>
        <div>
          <label class="form-label">地址</label>
          <el-input v-model="serverForm.host" placeholder="192.168.196.30" />
        </div>
        <div>
          <label class="form-label">端口</label>
          <el-input-number v-model="serverForm.port" :min="1" :max="65535" />
        </div>
        <div>
          <label class="form-label">SSH 用户名</label>
          <el-input v-model="serverForm.username" placeholder="ubuntu1" />
        </div>
        <div class="mon-form-wide">
          <label class="form-label">私钥（留空表示不修改）</label>
          <el-input
            v-model="serverForm.private_key"
            type="textarea"
            :rows="4"
            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
          />
        </div>
        <div class="mon-form-wide">
          <label class="form-label">主机指纹（测试连接后自动填入）</label>
          <el-input v-model="serverForm.host_key_fingerprint" placeholder="SHA256:..." />
        </div>
        <div>
          <label class="form-label">Prometheus 实例</label>
          <el-input v-model="serverForm.prometheus_instance" placeholder="192.168.196.30:9100" />
        </div>
        <div class="mon-config-switch">
          <label class="form-label">启用</label>
          <el-switch v-model="serverForm.enabled" />
        </div>
        <div class="mon-form-wide">
          <label class="form-label">承载服务（与故障的“服务”字段匹配，用于关联实时指标）</label>
          <el-select
            v-model="serverForm.service_names"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入服务名后回车，例如 order-service"
            class="w-full"
          >
            <el-option v-for="name in serverForm.service_names" :key="name" :label="name" :value="name" />
          </el-select>
        </div>
        <div class="mon-form-wide">
          <label class="form-label">备注</label>
          <el-input v-model="serverForm.notes" />
        </div>
      </div>
      <el-alert
        v-if="serverTest"
        :title="serverTest.ok ? '连接成功' : '连接失败'"
        :description="serverTest.message"
        :type="serverTest.ok ? 'success' : 'error'"
        :closable="false"
        show-icon
        class="mt-3"
      />
      <template #footer>
        <el-button :loading="serverTesting" @click="testServer">测试连接</el-button>
        <el-button @click="serverDialog = false">取消</el-button>
        <el-button type="primary" :loading="serverSaving" @click="saveServer">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mon-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.mon-panel {
  min-width: 0;
}
.mon-panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px 8px;
}
.mon-panel-head h3 {
  margin: 0;
  color: var(--ops-text);
  font-size: var(--ops-text-sm);
  font-weight: var(--ops-weight-semibold);
}
.mon-panel-value {
  margin: 6px 0 0;
  color: var(--ops-text);
  font-family: var(--ops-font-mono);
  font-size: 22px;
  font-weight: 500;
  letter-spacing: -1px;
}
.mon-panel-body {
  padding: 4px 14px 14px;
}
.mon-range {
  width: 130px;
}
.mon-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--ops-text-soft);
  font-size: var(--ops-text-xs);
  white-space: nowrap;
}
.mon-server-name {
  color: var(--ops-text);
  font-weight: var(--ops-weight-medium);
}
.mon-config-body {
  padding: 18px 20px;
}
.mon-config-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px 18px;
}
.mon-config-switch {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mon-config-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
}
.mon-template-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--ops-text);
  font-weight: var(--ops-weight-medium);
}
.mon-command {
  color: var(--ops-muted);
  font-family: var(--ops-font-mono);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.mon-drawer {
  display: flex;
  flex-direction: column;
}
.mon-drawer-actions {
  margin-top: 16px;
}
.mon-cmd-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
  color: var(--ops-muted);
  font-family: var(--ops-font-mono);
  font-size: 10px;
}
.mon-output {
  margin: 8px 0 0;
  padding: 12px;
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--ops-border-light);
  border-radius: 4px;
  background: var(--ops-surface-muted);
  color: var(--ops-text-soft);
  font-family: var(--ops-font-mono);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.mon-server-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
}
.mon-form-wide {
  grid-column: 1 / -1;
}
@media (max-width: 1100px) {
  .mon-grid,
  .mon-config-grid {
    grid-template-columns: 1fr;
  }
}
</style>
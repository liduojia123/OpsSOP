<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import { faultStatusMap, fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
import AiAssistantPanel from './AiAssistantPanel.vue'
import AppIcon from '../components/AppIcon.vue'
import type { AiStatus, Category, FaultDetail, RecordKind } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const fault = ref<FaultDetail | null>(null)
const categories = ref<Category[]>([])
const aiStatus = ref<AiStatus | null>(null)
const saving = ref(false)
const adding = ref(false)
const resolving = ref(false)
const toggling = ref(false)
const loadError = ref('')

const form = reactive({
  title: '',
  description: '',
  occurred_at: null as Date | null,
  environment: '',
  service: '',
  component_version: '',
  recent_changes: '',
  error_logs: '',
  attempted_actions: '',
  category_id: null as number | null,
  tags: [] as string[],
})

const recordForm = reactive({ kind: 'check_result' as RecordKind, content: '' })
const resolveForm = reactive({ actual_root_cause: '', handling_steps: '', recovery_verification: '' })
const resolveDialog = ref(false)

// 与后端一致:写操作仅 operator(本人)或 admin;reviewer 只读
const canEdit = computed(
  () =>
    fault.value &&
    fault.value.status !== 'resolved' &&
    (auth.role === 'admin' ||
      (auth.role === 'operator' && fault.value.created_by === auth.user?.id)),
)

function fillForm(data: FaultDetail) {
  form.title = data.title
  form.description = data.description
  form.occurred_at = data.occurred_at ? new Date(data.occurred_at) : null
  form.environment = data.environment
  form.service = data.service
  form.component_version = data.component_version
  form.recent_changes = data.recent_changes
  form.error_logs = data.error_logs
  form.attempted_actions = data.attempted_actions
  form.category_id = data.category_id
  form.tags = [...(data.tags ?? [])]
}

async function load() {
  const id = Number(route.params.id)
  try {
    const { data } = await client.get(`/api/faults/${id}`)
    fault.value = data
    loadError.value = ''
    fillForm(data)
  } catch (e) {
    // 明确展示加载失败并提供重试,不用空表单冒充已加载
    loadError.value = errMsg(e)
    throw e
  }
}

async function reload() {
  await load().catch(() => undefined)
}

function formPayload() {
  return {
    ...form,
    occurred_at: form.occurred_at ? new Date(form.occurred_at).toISOString() : null,
    tags: [...form.tags],
  }
}

async function save() {
  if (!fault.value || saving.value) return false
  saving.value = true
  try {
    // 只回写服务端元数据,不回填表单:请求期间继续输入的内容不被响应覆盖
    const { data } = await client.patch(`/api/faults/${fault.value.id}`, formPayload())
    fault.value.status = data.status
    fault.value.updated_at = data.updated_at
    ElMessage.success('已保存')
    return true
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留输入
    return false
  } finally {
    saving.value = false
  }
}

// 追加记录只更新 records 列表,不整页 reload,避免覆盖未保存的表单编辑
async function addRecord() {
  if (!fault.value || adding.value) return
  if (!recordForm.content.trim()) {
    ElMessage.warning('请输入记录内容')
    return
  }
  const submitted = recordForm.content
  adding.value = true
  try {
    const { data } = await client.post(`/api/faults/${fault.value.id}/records`, {
      kind: recordForm.kind,
      content: submitted,
    })
    // 仅当输入框内容仍是本次提交的内容时才清空;请求期间新输入保留
    if (recordForm.content === submitted) {
      recordForm.content = ''
    }
    fault.value.records.push(data)
    ElMessage.success('已追加记录')
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留输入
  } finally {
    adding.value = false
  }
}

async function toggleStatus() {
  if (!fault.value || !canEdit.value || toggling.value) return
  const next = fault.value.status === 'draft' ? 'open' : 'draft'
  toggling.value = true
  try {
    // 状态切换只改状态,不 reload 表单,未保存的描述/日志等编辑保留
    const { data } = await client.patch(`/api/faults/${fault.value.id}`, { status: next })
    fault.value.status = data.status
    fault.value.updated_at = data.updated_at
    ElMessage.success(next === 'open' ? '已转为处理中' : '已转为草稿')
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    toggling.value = false
  }
}

// AI 建议采纳:仅填入表单(人工可继续修改),不自动保存、不直接改写故障
function applyAiSuggestion(payload: { category_id: number | null; tags: string[] }) {
  if (!canEdit.value) return
  if (payload.category_id != null) form.category_id = payload.category_id
  if (payload.tags.length) {
    const merged = new Set([...form.tags, ...payload.tags])
    form.tags = [...merged].slice(0, 16) // 有限标签,避免无限膨胀
  }
}

// 确认解决前先保存表单并等待成功,确保案例取到最新故障内容;失败则中止并保留输入
async function doResolve() {
  if (!fault.value || resolving.value) return
  const faultId = fault.value.id // ID 快照:多步请求不随状态变化
  if (!resolveForm.actual_root_cause.trim() || !resolveForm.handling_steps.trim() || !resolveForm.recovery_verification.trim()) {
    ElMessage.warning('确认解决须填写实际根因、处理步骤和恢复验证')
    return
  }
  resolving.value = true
  try {
    await client.patch(`/api/faults/${faultId}`, formPayload())
    const { data } = await client.post(`/api/faults/${faultId}/resolve`, { ...resolveForm })
    resolveDialog.value = false
    ElMessage.success('已确认解决,生成待审核案例')
    // 更新记录状态但不回填表单,请求期间继续输入的内容不被覆盖
    if (fault.value && fault.value.id === faultId) {
      fault.value = data
    }
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留对话框与已填内容
  } finally {
    resolving.value = false
  }
}

// 分类与 AI 状态独立加载:任一失败不牵连另一个,
// AI 状态失败时保持 null,面板禁用模型调用并提供重试入口
async function loadCategories() {
  try {
    const { data } = await client.get('/api/categories')
    categories.value = data
  } catch {
    /* 分类下拉失败不阻塞业务 */
  }
}

async function loadAiStatus() {
  try {
    const { data } = await client.get('/api/system/ai-status')
    aiStatus.value = data
  } catch {
    /* 状态未知:面板在 aiStatus=null 时禁用自动模型调用,可重试 */
    aiStatus.value = null
  }
}

onMounted(async () => {
  await Promise.all([loadCategories(), loadAiStatus()])
  await load().catch((e) => ElMessage.error(errMsg(e)))
})
</script>

<template>
  <div class="ops-page fault-detail-page">
    <div class="ops-page-header fault-page-header">
      <div class="fault-page-title">
        <el-button text class="ops-back-link" @click="router.push('/faults')">← 返回故障列表</el-button>
        <div class="ops-eyebrow">INCIDENT WORKSPACE <span v-if="fault">/ #{{ fault.id }}</span></div>
        <div class="fault-title-line">
          <h1>{{ fault?.title ?? '加载中' }}</h1>
          <el-tag v-if="fault" size="small" :type="(faultStatusMap[fault.status].type as any)">
            {{ faultStatusMap[fault.status].label }}
          </el-tag>
        </div>
        <p class="ops-page-description">保留原始证据，记录每一步排查，让结论有据可循。</p>
      </div>
      <div class="ops-page-actions fault-page-actions">
        <el-button v-if="canEdit && fault?.status === 'draft'" :loading="toggling" @click="toggleStatus">开始处理</el-button>
        <el-button v-if="canEdit && fault?.status === 'open'" :loading="toggling" @click="toggleStatus">转为草稿</el-button>
        <el-button v-if="canEdit" type="primary" :loading="saving" @click="save">保存</el-button>
        <el-button v-if="canEdit && fault && fault.status !== 'resolved'" type="success" plain @click="resolveDialog = true">
          <AppIcon name="check-circle" :size="16" /> 确认解决
        </el-button>
      </div>
    </div>

    <div v-if="loadError" class="ops-panel fault-load-error">
      <el-alert type="error" :title="loadError" :closable="false" show-icon />
      <div class="fault-load-error-actions">
        <el-button type="primary" plain @click="reload">重试</el-button>
        <el-button @click="router.push('/faults')">返回故障列表</el-button>
      </div>
    </div>

    <div v-else class="fault-workspace">
      <!-- 左侧:录入区 -->
      <div class="fault-evidence-column">
        <section class="ops-panel fault-evidence-panel">
          <div class="ops-section-header">
            <div class="fault-section-heading"><span class="section-icon"><AppIcon name="files" :size="18" /></span><div><h2>故障信息</h2><p>原始描述与现场证据</p></div></div>
            <span class="section-kicker">EVIDENCE</span>
          </div>
          <el-form label-position="top" class="fault-evidence-form" :disabled="!canEdit">
            <el-form-item label="标题">
              <el-input v-model="form.title" />
            </el-form-item>
            <el-form-item label="详细描述">
              <el-input v-model="form.description" type="textarea" :rows="3" />
            </el-form-item>
            <div class="fault-properties-grid">
              <el-form-item label="发生时间">
                <el-date-picker
                  v-model="form.occurred_at"
                  type="datetime"
                  placeholder="故障发生时间(可选)"
                  class="!w-full"
                />
              </el-form-item>
              <el-form-item label="环境">
                <el-input v-model="form.environment" />
              </el-form-item>
              <el-form-item label="服务">
                <el-input v-model="form.service" />
              </el-form-item>
              <el-form-item label="组件版本">
                <el-input v-model="form.component_version" />
              </el-form-item>
              <el-form-item label="分类">
                <el-select v-model="form.category_id" clearable class="w-full">
                  <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="标签">
                <el-select
                  v-model="form.tags"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  placeholder="输入后回车添加标签"
                  class="w-full"
                >
                  <el-option v-for="t in form.tags" :key="t" :label="t" :value="t" />
                </el-select>
              </el-form-item>
            </div>
            <el-form-item label="近期变更">
              <el-input v-model="form.recent_changes" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="报错日志">
              <el-input v-model="form.error_logs" class="fault-log-input" type="textarea" :rows="6" />
            </el-form-item>
            <el-form-item label="已尝试操作">
              <el-input v-model="form.attempted_actions" type="textarea" :rows="2" />
            </el-form-item>
          </el-form>
        </section>

        <!-- 追加记录 -->
        <section class="ops-panel fault-record-panel">
          <div class="ops-section-header"><div class="fault-section-heading"><span class="section-icon"><AppIcon name="history" :size="18" /></span><div><h2>检查结果与处理记录</h2><p>把现场发现留在时间线上</p></div></div><span v-if="fault" class="record-count">{{ fault.records.length }} 条记录</span></div>
          <div v-if="!fault || fault.records.length === 0" class="ops-empty is-quiet mb-3">
            <AppIcon name="history" :size="22" />
            <div><strong>暂无检查记录</strong><span>在下方补充检查结果与处理动作，形成可追溯的时间线。</span></div>
          </div>
          <div v-else class="record-timeline">
            <div
              v-for="r in fault.records"
              :key="r.id"
              class="record-timeline-item ops-result-in"
            >
              <div class="record-meta">
                <el-tag size="small" :type="r.kind === 'check_result' ? 'primary' : 'warning'" effect="plain">
                  {{ r.kind === 'check_result' ? '检查结果' : '处理记录' }}
                </el-tag>
                <span>{{ fmtTime(r.created_at) }}</span>
              </div>
              <div class="record-content">{{ r.content }}</div>
            </div>
          </div>
          <div v-if="canEdit && fault && fault.status !== 'resolved'" class="record-composer">
            <el-select v-model="recordForm.kind" class="!w-28">
              <el-option label="检查结果" value="check_result" />
              <el-option label="处理记录" value="handling_note" />
            </el-select>
            <el-input
              v-model="recordForm.content"
              type="textarea"
              :rows="2"
              placeholder="补充检查结果或处理动作…"
            />
            <el-button type="primary" :loading="adding" @click="addRecord">追加</el-button>
          </div>
        </section>

        <!-- 人工确认结果 -->
        <div v-if="fault && fault.status === 'resolved'" class="ops-panel confirmed-panel">
          <div class="ops-section-header"><div class="fault-section-heading"><span class="section-icon"><AppIcon name="check-circle" :size="18" /></span><div><h2>人工确认结果</h2><p>已确认事实 · 由人员填写并确认</p></div></div></div>
          <div class="confirmed-fields">
            <div>
              <div class="confirmed-label">实际根因</div>
              <div class="confirmed-value">{{ fault.actual_root_cause }}</div>
            </div>
            <div>
              <div class="confirmed-label">处理步骤</div>
              <div class="confirmed-value">{{ fault.handling_steps }}</div>
            </div>
            <div>
              <div class="confirmed-label">恢复验证</div>
              <div class="confirmed-value">{{ fault.recovery_verification }}</div>
            </div>
            <div class="ops-text-faint confirmed-time">确认时间: {{ fmtTime(fault.resolved_at) }}</div>
          </div>
        </div>
      </div>

      <!-- 右侧:AI 排查助手 -->
      <AiAssistantPanel
        v-if="fault"
        :fault-id="fault.id"
        :form="form"
        :records="fault.records"
        :categories="categories"
        :ai-status="aiStatus"
        :can-edit="!!canEdit"
        @apply-suggestion="applyAiSuggestion"
        @retry-status="loadAiStatus"
      />
      <aside v-else class="fault-assistant-placeholder">
        <div class="page-card flex-1 flex items-center justify-center ops-text-muted">
          加载中…
        </div>
      </aside>
    </div>

    <!-- 确认解决对话框 -->
    <el-dialog v-model="resolveDialog" title="人工确认解决" width="560px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        class="mb-4"
        title="以下内容必须由人工填写。AI 推测(候选原因)不会自动填入,也不会被当作已确认事实。"
      />
      <el-form label-position="top">
        <el-form-item label="实际根因" required>
          <el-input v-model="resolveForm.actual_root_cause" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="处理步骤" required>
          <el-input v-model="resolveForm.handling_steps" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="恢复验证" required>
          <el-input v-model="resolveForm.recovery_verification" type="textarea" :rows="3" placeholder="如何确认服务已恢复" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resolveDialog = false">取消</el-button>
        <el-button type="success" :loading="resolving" @click="doResolve">确认解决并生成案例</el-button>
      </template>
    </el-dialog>
  </div>
</template>


<style scoped>
.fault-detail-page { display: flex; flex-direction: column; gap: 22px; height: 100%; min-height: 0; overflow: hidden; }
.fault-page-header { flex: 0 0 auto; align-items: flex-end; gap: 20px; }
.fault-page-title { min-width: 0; }
.fault-page-title .ops-back-link { margin: 0 0 10px -6px; }
.fault-title-line { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 7px; }
.fault-title-line h1 { max-width: 820px; font-size: 25px; line-height: 1.45; letter-spacing: -0.02em; }
.fault-page-actions { flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; padding-bottom: 3px; }
.fault-page-actions :deep(.el-button) { margin-left: 0; gap: 5px; }
.fault-workspace { display: grid; grid-template-columns: minmax(0, 1fr) minmax(350px, 400px); gap: 22px; flex: 1; min-height: 0; }
.fault-evidence-column { display: flex; min-width: 0; flex-direction: column; gap: 20px; overflow: auto; padding-right: 3px; padding-bottom: 24px; }
.fault-evidence-column > .ops-panel { flex-shrink: 0; padding: 24px; }
.fault-section-heading { display: flex; align-items: center; gap: 11px; }
.fault-section-heading h2 { margin: 0; color: var(--ops-text); font-size: 15px; font-weight: 650; }
.fault-section-heading p { margin: 3px 0 0; color: var(--ops-muted); font-size: 12px; }
.section-icon { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }
.section-kicker { color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 10px; font-weight: 600; letter-spacing: 1.5px; }
.fault-evidence-form { margin-top: 22px; }
.fault-evidence-form :deep(.el-form-item) { margin-bottom: 20px; }
.fault-evidence-form :deep(.el-form-item__label) { margin-bottom: 8px; color: var(--ops-text); font-size: 12px; font-weight: 600; }
.fault-properties-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 18px; padding-top: 2px; }
.fault-log-input :deep(textarea) { background: var(--ops-surface-warm); color: var(--ops-text-soft); font-family: var(--ops-font-mono); font-size: 12px; line-height: 1.8; }
.record-count { color: var(--ops-muted); font-family: var(--ops-font-mono); font-size: 11px; white-space: nowrap; }
.record-timeline { margin: 20px 0; padding-left: 17px; border-left: 1px solid var(--ops-border); }
.record-timeline-item { position: relative; padding: 0 0 22px 7px; }
.record-timeline-item:last-child { padding-bottom: 0; }
.record-timeline-item::before { content: ''; position: absolute; top: 7px; left: -22px; width: 8px; height: 8px; border: 2px solid var(--ops-surface); border-radius: 50%; background: var(--ops-brand); box-shadow: 0 0 0 1px var(--ops-border); }
.record-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 10px; }
.record-content { color: var(--ops-text-soft); font-size: 13px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.record-composer { display: flex; align-items: flex-start; gap: 10px; padding-top: 18px; border-top: 1px solid var(--ops-border-light); }
.record-composer :deep(.el-textarea) { flex: 1; }
.record-composer :deep(.el-select) { flex-shrink: 0; }
.confirmed-panel { border-color: var(--ops-warm-line); background: var(--ops-surface-muted); }
.confirmed-fields { margin-top: 22px; }
.confirmed-fields > div { padding-bottom: 15px; border-bottom: 1px solid var(--ops-border-light); }
.confirmed-fields > div:last-child { padding-bottom: 0; border: 0; }
.confirmed-label { margin-bottom: 6px; color: var(--ops-muted); font-size: 11px; }
.confirmed-value { color: var(--ops-text); font-size: 13px; line-height: 1.85; white-space: pre-wrap; overflow-wrap: anywhere; }
.confirmed-time { margin-top: 14px; font-family: var(--ops-font-mono); font-size: 10px; }
.fault-assistant-placeholder { min-height: 0; }
.fault-load-error { display: flex; flex-direction: column; gap: 16px; max-width: 760px; padding: 24px; }
.fault-load-error-actions { display: flex; gap: 8px; }
@media (min-width: 1500px) { .fault-workspace { grid-template-columns: minmax(0, 1fr) 420px; } }
@media (max-width: 1180px) {
  .fault-page-header { align-items: flex-start; flex-direction: column; }
  .fault-page-actions { justify-content: flex-start; }
  .fault-workspace { grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; }
  .fault-evidence-column > .ops-panel { padding: 20px; }
}
@media (max-width: 1020px) {
  .fault-detail-page { height: auto; overflow: visible; }
  .fault-workspace { grid-template-columns: minmax(0, 1fr); }
  .fault-evidence-column { overflow: visible; padding-right: 0; padding-bottom: 0; }
}
@media (max-width: 600px) {
  .fault-properties-grid { grid-template-columns: minmax(0, 1fr); }
  .record-composer { flex-wrap: wrap; }
  .record-composer :deep(.el-textarea) { flex-basis: calc(100% - 125px); }
  .record-composer :deep(.el-button) { margin-left: auto; }
  .section-kicker { display: none; }
  .fault-title-line h1 { font-size: 21px; }
}
</style>







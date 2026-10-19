<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import { caseStatusMap, fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { AiStatus, Case, CaseStatus, Category, SopGenerateAiResponse, SopGenerationRun } from '../types'

const router = useRouter()

const auth = useAuthStore()
const cases = ref<Case[]>([])
const categories = ref<Category[]>([])
const loading = ref(false)
const filters = reactive({ q: '', status: '' as '' | CaseStatus })

const detailVisible = ref(false)
const current = ref<Case | null>(null)
const reviewComment = ref('')

// ===== AI 生成 SOP 草稿(第三阶段第一批)=====
// 与模板生成分开;生成中防重复点击;失败持久展示原因与人工重试;
// 同一次「点击生成」的重试沿用 request_token(不重复创建草稿/重复计费),
// 主动再次生成使用新 token(可再建新草稿);过期响应不应用到其他案例。
const aiStatus = ref<AiStatus | null>(null)
const aiGenerating = ref(false)
const aiError = ref<{ message: string; code?: string } | null>(null)
const aiLastRun = ref<SopGenerationRun | null>(null)
let aiRequestToken = ''
let aiGenSeq = 0
// 组件卸载标记:迟到响应不得再触发跳转/通知/状态写入(复审 P2-2)
let disposed = false

onBeforeUnmount(() => {
  disposed = true
  aiGenSeq++ // 使在途响应整体失效
})

const aiDisabled = computed(() => !aiStatus.value || aiStatus.value.ai_mode === 'disabled')
const aiMode = computed(() => aiStatus.value?.ai_mode ?? '')

function runUsageText(run: SopGenerationRun): string {
  const u = run.usage
  if (!u || Object.keys(u).length === 0) return '用量未知'
  const fmt = (v: unknown) => (typeof v === 'number' ? String(v) : '未知')
  return `用量:输入 ${fmt(u.prompt_tokens)} / 输出 ${fmt(u.completion_tokens)} / 合计 ${fmt(u.total_tokens)}`
}

function newRequestToken(): string {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `tok-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function generateSopAi(retry = false) {
  if (disposed) return // 卸载后不再发起请求
  if (!current.value || aiGenerating.value) return // 生成中防重复点击
  const caseId = current.value.id
  if (!retry || !aiRequestToken) {
    aiRequestToken = newRequestToken() // 主动再次生成 → 新 token → 可再建新草稿
  }
  const seq = ++aiGenSeq
  aiGenerating.value = true
  try {
    const { data } = await client.post<SopGenerateAiResponse>(
      `/api/cases/${caseId}/generate-sop-ai`,
      { request_token: aiRequestToken },
      { timeout: 90000 },
    )
    // 过期响应不应用到其他案例/新请求;卸载后不再执行任何副作用(跳转/通知/状态)
    if (disposed || seq !== aiGenSeq || current.value?.id !== caseId) return
    aiLastRun.value = data.run
    if (data.run.status === 'success' && data.sop) {
      // HTTP 201 不是成功判据:业务成功才进入编辑页
      aiError.value = null
      ElMessage.success(
        data.run.is_demo
          ? '已生成演示 SOP 草稿(模拟数据),请人工核对'
          : '已生成 SOP 草稿,请人工核对、补充后提交审核',
      )
      detailVisible.value = false
      void router.push(`/sops/${data.sop.id}`)
    } else {
      // 业务失败:持久展示原因与重试入口,不跳转、不丢页面输入
      aiError.value = {
        message: data.run.error_message?.trim() || '生成失败(未记录原因)',
        code: data.run.error_code,
      }
    }
  } catch (e) {
    if (disposed || seq !== aiGenSeq || current.value?.id !== caseId) return
    aiError.value = { message: errMsg(e) }
  } finally {
    if (!disposed && seq === aiGenSeq) aiGenerating.value = false
  }
}

function retryGenerateSopAi() {
  // 同一次请求的重试:沿用 request_token,不重复创建草稿
  void generateSopAi(true)
}

function resetAiGenerate() {
  aiGenSeq++ // 使在途响应整体失效
  aiGenerating.value = false
  aiError.value = null
  aiLastRun.value = null
  aiRequestToken = ''
}

function fieldSourcesText(run: SopGenerationRun): string {
  const items = (run.field_sources ?? []) as Array<{ target: string; evidence_ids: string[] }>
  if (!items.length) return ''
  return items.map((s) => `${s.target} ← ${(s.evidence_ids ?? []).join('、')}`).join(';')
}

async function load() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    if (filters.q) params.q = filters.q
    if (filters.status) params.status = filters.status
    const { data } = await client.get('/api/cases', { params })
    cases.value = data
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

function openDetail(row: Case) {
  if (current.value?.id !== row.id) resetAiGenerate() // 切换案例:旧生成结果/在途响应失效
  current.value = row
  reviewComment.value = ''
  detailVisible.value = true
}

function categoryName(id: number | null) {
  return categories.value.find((c) => c.id === id)?.name ?? '待分类'
}

async function review(action: 'approve' | 'return') {
  if (!current.value) return
  if (action === 'return' && !reviewComment.value.trim()) {
    ElMessage.warning('退回必须填写审核意见')
    return
  }
  try {
    await client.post(`/api/cases/${current.value.id}/${action}`, { comment: reviewComment.value })
    ElMessage.success(action === 'approve' ? '已通过' : '已退回')
    detailVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function generateSop() {
  if (!current.value) return
  try {
    const { data } = await client.post(`/api/cases/${current.value.id}/generate-sop`)
    if (disposed) return // 卸载后不再跳转/通知(复审 P2-2 同一契约)
    ElMessage.success('已生成模板 SOP 草稿(字段拷贝),请人工补全')
    detailVisible.value = false
    void router.push(`/sops/${data.id}`)
  } catch (e) {
    if (disposed) return
    ElMessage.error(errMsg(e))
  }
}

// 退回案例的三项人工确认字段均可修改(后端只允许 returned 状态编辑)
type ConfirmField = 'confirmed_root_cause' | 'handling_steps' | 'recovery_verification'

async function editField(field: ConfirmField, label: string) {
  if (!current.value) return
  const { value } = await ElMessageBox.prompt(`修改${label}`, '编辑案例', {
    inputValue: current.value[field],
    inputType: 'textarea',
  }).catch(() => ({ value: null }))
  if (value === null) return
  try {
    await client.patch(`/api/cases/${current.value.id}`, { [field]: value })
    ElMessage.success('已更新')
    await load()
    current.value = cases.value.find((c) => c.id === current.value?.id) ?? current.value
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

// 与后端一致:仅 returned 可编辑,operator 只能改自己创建的案例
const canEditCurrent = computed(
  () =>
    current.value &&
    current.value.status === 'returned' &&
    (auth.role === 'admin' ||
      (auth.role === 'operator' && current.value.created_by === auth.user?.id)),
)

async function resubmit() {
  if (!current.value) return
  try {
    await client.post(`/api/cases/${current.value.id}/submit`)
    ElMessage.success('已重新提交审核')
    detailVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(async () => {
  // AI 状态独立加载:失败不阻塞案例列表(生成入口按状态禁用)
  void client
    .get<AiStatus>('/api/system/ai-status')
    .then(({ data }) => {
      aiStatus.value = data
    })
    .catch(() => {
      aiStatus.value = null
    })
  const { data } = await client.get('/api/categories')
  categories.value = data
  await load()
})
</script>

<template>
  <div class="p-6">
    <h1 class="text-xl font-bold text-slate-800 mb-4">案例库</h1>

    <div class="page-card mb-4">
      <div class="flex flex-wrap gap-3 items-end">
        <div>
          <label class="form-label">关键词</label>
          <el-input v-model="filters.q" placeholder="标题/现象/根因" clearable class="!w-56" @keyup.enter="load" />
        </div>
        <div>
          <label class="form-label">状态</label>
          <el-select v-model="filters.status" clearable placeholder="全部" class="!w-32">
            <el-option label="待审核" value="pending_review" />
            <el-option label="已通过" value="approved" />
            <el-option label="已退回" value="returned" />
          </el-select>
        </div>
        <el-button type="primary" plain @click="load">查询</el-button>
      </div>
    </div>

    <div class="page-card">
      <el-table :data="cases" v-loading="loading" empty-text="暂无案例(故障确认解决后生成)">
        <el-table-column label="标题" min-width="200">
          <template #default="{ row }">
            <a class="text-blue-600 hover:underline cursor-pointer" @click="openDetail(row)">
              {{ row.title }}
            </a>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="130">
          <template #default="{ row }">{{ categoryName(row.category_id) }}</template>
        </el-table-column>
        <el-table-column prop="service" label="服务" width="120" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="(caseStatusMap[row.status as CaseStatus].type as any)">
              {{ caseStatusMap[row.status as CaseStatus].label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <el-drawer v-model="detailVisible" title="案例详情" size="48%">
      <template v-if="current">
        <el-alert
          v-if="current.status === 'approved'"
          type="success"
          :closable="false"
          title="已审核通过的正式诊断依据"
          class="mb-4"
        />
        <el-alert
          v-if="current.status === 'returned'"
          type="error"
          :closable="false"
          :title="`已退回,审核意见: ${current.review_comment}`"
          class="mb-4"
        />

        <div class="space-y-4 text-sm">
          <div>
            <div class="text-gray-500 mb-1">标题</div>
            <div class="font-medium text-slate-800">{{ current.title }}</div>
          </div>
          <div>
            <div class="text-gray-500 mb-1">现象(原始描述)</div>
            <div class="p-2 bg-slate-50 rounded whitespace-pre-wrap">{{ current.symptom || '-' }}</div>
          </div>
          <div>
            <div class="text-gray-500 mb-1">人工确认根因</div>
            <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ current.confirmed_root_cause }}</div>
          </div>
          <div>
            <div class="text-gray-500 mb-1">处理步骤</div>
            <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ current.handling_steps }}</div>
          </div>
          <div>
            <div class="text-gray-500 mb-1">恢复验证</div>
            <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ current.recovery_verification }}</div>
          </div>
          <div class="text-xs text-gray-400">
            环境: {{ current.environment || '-' }} · 服务: {{ current.service || '-' }} ·
            版本: {{ current.component_version || '-' }}
          </div>
        </div>

        <div class="mt-6 space-x-2">
          <!-- 审核操作 -->
          <template v-if="auth.role === 'reviewer' || auth.role === 'admin'">
            <template v-if="current.status === 'pending_review'">
              <el-input
                v-model="reviewComment"
                type="textarea"
                :rows="2"
                placeholder="审核意见(退回时必填)"
                class="mb-2"
              />
              <el-button type="success" @click="review('approve')">通过</el-button>
              <el-button type="danger" @click="review('return')">退回</el-button>
            </template>
          </template>

          <!-- 提交人操作 -->
          <template v-if="canEditCurrent">
            <el-button type="primary" @click="resubmit">重新提交审核</el-button>
            <el-button @click="editField('confirmed_root_cause', '人工确认根因')">修改根因</el-button>
            <el-button @click="editField('handling_steps', '处理步骤')">修改处理步骤</el-button>
            <el-button @click="editField('recovery_verification', '恢复验证')">修改恢复验证</el-button>
          </template>
          <template v-if="auth.role === 'operator' || auth.role === 'admin'">
            <template
              v-if="current.status === 'approved' && (current.created_by === auth.user?.id || auth.role === 'admin')"
            >
              <!-- 两个生成入口名称清楚区分:模板(字段拷贝)与 AI(模型整理) -->
              <el-button type="primary" plain @click="generateSop">模板生成草稿</el-button>
              <el-button
                type="success"
                plain
                :loading="aiGenerating"
                :disabled="aiDisabled"
                @click="generateSopAi(false)"
              >
                AI 生成草稿
              </el-button>
            </template>
          </template>

          <!-- AI 生成:模式说明(云模式说明出网范围,无需逐次确认弹窗)与结果/失败展示 -->
          <div class="mt-3 space-y-2">
            <div
              v-if="aiMode === 'cloud' || aiMode === 'local'"
              class="text-xs leading-5 p-2 rounded bg-amber-50 text-amber-800 border border-amber-200"
            >
              AI 生成将向已配置的模型服务发送脱敏后的案例材料(来源快照),生成结果须人工核对;
              不会自动提交审核或发布。
            </div>
            <div
              v-else-if="aiMode === 'demo'"
              class="text-xs leading-5 p-2 rounded bg-orange-50 text-orange-700 border border-orange-200"
            >
              演示模式:AI 生成内容为模拟数据,不代表真实模型能力。
            </div>
            <div
              v-else-if="aiStatus && aiDisabled"
              class="text-xs leading-5 p-2 rounded bg-slate-50 text-slate-500"
            >
              AI 生成未启用;模板生成与手工编辑不受影响。
            </div>
            <div v-else-if="!aiStatus" class="text-xs leading-5 p-2 rounded bg-slate-50 text-slate-500">
              AI 状态未知:已禁用 AI 生成入口(未确认模式与出网提示前不发送)。
            </div>

            <div v-if="aiGenerating" class="text-xs text-blue-600 bg-blue-50 rounded px-2 py-1.5">
              AI 生成中(整理来源材料 → 校验 → 创建草稿)…
            </div>

            <!-- 业务 failed 与请求失败统一:持久显示原因/调用信息/人工重试入口 -->
            <div
              v-if="aiError"
              class="text-xs text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1.5"
            >
              <div class="font-medium mb-1">AI 生成失败:{{ aiError.message }}</div>
              <div v-if="aiLastRun" class="mb-1 text-red-400">
                调用次数:{{ aiLastRun.call_count }} · 延迟:{{ aiLastRun.latency_ms }}ms ·
                {{ runUsageText(aiLastRun) }}
                <span v-if="aiLastRun.is_demo"> · 演示数据</span>
              </div>
              <div v-if="aiLastRun && fieldSourcesText(aiLastRun)" class="mb-1 text-red-400">
                字段来源依据:{{ fieldSourcesText(aiLastRun) }}
              </div>
              <el-button size="small" type="danger" plain :loading="aiGenerating" @click="retryGenerateSopAi">
                重试(同一次请求)
              </el-button>
              <span class="text-red-400 ml-2">输入不受影响;也可重新点击「AI 生成草稿」再次生成</span>
            </div>

            <!-- 生成成功但未跳转(如过期)时的运行信息;失败时的待补信息一并可见 -->
            <div
              v-else-if="aiLastRun"
              class="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-2 py-1.5"
            >
              <div>
                状态:成功 · 调用次数:{{ aiLastRun.call_count }} · 延迟:{{ aiLastRun.latency_ms }}ms ·
                {{ runUsageText(aiLastRun) }}
              </div>
              <div v-if="aiLastRun.is_demo" class="text-orange-700 mt-1">
                <b>演示数据 / 模拟生成</b>:不代表真实模型能力。
              </div>
              <div v-if="aiLastRun.missing_information?.length" class="mt-1">
                待补信息:{{ aiLastRun.missing_information.join('；') }}
              </div>
              <div v-if="fieldSourcesText(aiLastRun)" class="mt-1">
                字段来源依据:{{ fieldSourcesText(aiLastRun) }}
              </div>
            </div>
          </div>
          <div v-if="current.status === 'pending_review'" class="text-xs text-gray-400 mt-2">
            待审核内容已锁定审核快照;如需修改请等待退回。
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 右侧 AI 排查助手面板。
 *
 - 输入停顿约 2 秒自动分析,可关闭自动并手动触发;空输入/无实质变化不调用。
 * - 请求绑定故障 ID、输入版本(内容指纹)与模型配置版本:
 *   过期响应(请求序号落后、故障切换、输入已变更)不覆盖新结果,只标记过期。
 * - 失败可重试且不丢输入;429 暂停自动分析,不立即循环请求。
 * - 演示数据逐条标注;usage 缺失显示未知,不估算费用;命令仅展示与复制,不执行。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import axios from 'axios'
import client, { errMsg } from '../api'
import { fmtTime } from '../labels'
import { renderSafeMd } from '../safeMd'
import AppIcon from '../components/AppIcon.vue'
import { sourcePath, sourceTagLabel, sourceVersionText, sourceViewLabel } from '../sourceLinks'
import type {
  AiAnalysis,
  AiStatus,
  AnalysisReport,
  AnalyzeResponse,
  Category,
  FaultRecord,
  SearchHit,
  SearchResponse,
} from '../types'

/** 与 FaultDetailView 的表单对象结构一致(只读快照来源) */
interface FormModel {
  title: string
  description: string
  environment: string
  service: string
  component_version: string
  recent_changes: string
  error_logs: string
  attempted_actions: string
  category_id: number | null
  tags: string[]
}

const props = defineProps<{
  faultId: number
  form: FormModel
  records: FaultRecord[]
  categories: Category[]
  aiStatus: AiStatus | null
  canEdit: boolean
}>()

const emit = defineEmits<{
  (e: 'apply-suggestion', payload: { category_id: number | null; tags: string[] }): void
  (e: 'retry-status'): void // AI 状态未知时请求父页重新加载
}>()

// ===== 分析状态 =====
const autoAnalyze = ref(true)
const analyzing = ref(false)
const analysis = ref<AiAnalysis | null>(null)
const reused = ref(false)
const stale = ref(false)
const staleReason = ref('')
const error = ref<{ message: string; status?: number } | null>(null)
const rateLimited = ref(false) // 429 后暂停自动分析,避免立即循环请求
const emptyHint = ref(false)
const history = ref<AiAnalysis[]>([])
const historyOpen = ref(false)

// ===== 知识检索状态(须在下方 immediate watcher 之前初始化,避免 TDZ,审查 P1 #5)=====
const searchQuery = ref('')
const searching = ref(false)
const searchDone = ref(false)
const searchError = ref('')
const hits = ref<SearchHit[]>([])
// 实际检索方式与降级信息(不把关键词结果冒充语义检索,服务错误不显示为无匹配)
const searchMode = ref('keyword')
const searchDegraded = ref(false)
const searchDegradeReason = ref('')

// 请求序号 + AbortController:倒序响应/切换故障后旧响应一律丢弃
let requestSeq = 0
// 检索请求序号与最近一次提交的查询:旧响应后到不得覆盖新结果
let searchSeq = 0
let lastSearchQuery = ''
let controller: AbortController | null = null
let debounceTimer: ReturnType<typeof setTimeout> | undefined
let lastAnalyzedSig = ''
// 组件卸载标记:使在途请求与待排队任务整体失效
let disposed = false

// 状态未知(aiStatus 为 null)时视为不可调用:未确认模式与出网提示前不自动外发
const aiDisabled = computed(
  () => !props.aiStatus || (props.aiStatus.ai_mode === 'disabled' && !props.aiStatus.user_ai_enabled),
)
/** 用户自填厂商配置:全局未启用时也能用(后端按用户配置调用) */
const userProviderLabel = computed(() => {
  if (!props.aiStatus?.user_ai_enabled) return ''
  const names: Record<string, string> = { deepseek: 'DeepSeek', minimax: 'MiniMax', mimo: '小米 MiMo' }
  return names[props.aiStatus.user_provider] || props.aiStatus.user_provider
})
const router = useRouter()

function openSettings() {
  router.push('/ai-settings')
}
const aiStatusMissing = computed(() => !props.aiStatus)
const isDemo = computed(() => analysis.value?.is_demo ?? false)

// ===== 输入版本(内容指纹)=====
function fnv1a(input: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16).padStart(8, '0')
}

function inputSigOf(): string {
  const p = props.form
  return fnv1a(
    JSON.stringify([
      p.title,
      p.description,
      p.environment,
      p.service,
      p.component_version,
      p.recent_changes,
      p.error_logs,
      p.attempted_actions,
      (p.tags ?? []).join(','),
      p.category_id ?? '',
      props.records.map((r) => `${r.id}:${r.content}`).join('|'),
    ]),
  )
}

const inputSig = computed(() => inputSigOf())

function hasSubstance(): boolean {
  const p = props.form
  return Boolean(
    p.title.trim() ||
      p.description.trim() ||
      p.error_logs.trim() ||
      p.recent_changes.trim() ||
      p.attempted_actions.trim() ||
      p.environment.trim() ||
      p.service.trim() ||
      p.component_version.trim() ||
      props.records.some((r) => r.content.trim()),
  )
}

function formPatch() {
  const p = props.form
  return {
    title: p.title,
    description: p.description,
    environment: p.environment,
    service: p.service,
    component_version: p.component_version,
    recent_changes: p.recent_changes,
    error_logs: p.error_logs,
    attempted_actions: p.attempted_actions,
    tags: [...p.tags],
    category_id: p.category_id,
  }
}

function markStale(reason: string) {
  if (analysis.value) {
    stale.value = true
    staleReason.value = reason
  }
}

// ===== 触发分析 =====
// 在途分析期间的最新变更:旧请求结束后重新排队,不丢弃最后一次输入(审查 P2 #7)
let pendingAuto = false

async function analyze(trigger: 'auto' | 'manual') {
  if (disposed) return // 卸载后不再发起或补发请求
  if (aiStatusMissing.value) {
    // 状态未知:不知道当前模式与是否允许出网,一律不调用
    ElMessage.warning('AI 状态未知,已暂停模型调用;请重新加载状态后再试。')
    return
  }
  if (aiDisabled.value) {
    ElMessage.warning('AI 未启用,可使用下方知识检索;如需分析请在部署配置中启用模型。')
    return
  }
  // 自动触发在实际发送前复检开关/限流(审查 P2 #8):关闭开关后已排定的计时器不再发请求
  if (trigger === 'auto' && (!autoAnalyze.value || rateLimited.value)) return
  if (analyzing.value && trigger === 'auto') {
    pendingAuto = true // 记录最新待分析输入,旧请求结束后重新排队
    return
  }
  const faultId = props.faultId
  const sentSig = inputSigOf()
  if (!hasSubstance()) {
    emptyHint.value = true
    return
  }
  emptyHint.value = false
  if (trigger === 'auto' && sentSig === lastAnalyzedSig) return // 无实质变化不调用

  const seq = ++requestSeq
  controller?.abort() // 取消可取消的旧请求
  controller = new AbortController()
  lastAnalyzedSig = sentSig
  analyzing.value = true
  error.value = null

  try {
    const { data } = await client.post<AnalyzeResponse>(
      `/api/faults/${faultId}/analyze`,
      { trigger_type: trigger, use_cache: true, input: formPatch() },
      { signal: controller.signal, timeout: 90000 },
    )
    // 过期响应不覆盖新结果:序号落后或已切换故障直接丢弃
    if (seq !== requestSeq || props.faultId !== faultId) return

    analysis.value = data.analysis
    reused.value = data.reused
    // 响应回来时输入/配置是否已变:变了则展示但标记过期
    const nowSig = inputSigOf()
    const cfgNow = props.aiStatus?.model_config_version ?? ''
    if (nowSig !== sentSig) {
      stale.value = true
      staleReason.value = '分析期间输入已变更,当前报告对应较早输入'
    } else if (cfgNow && data.analysis.model_config_version && cfgNow !== data.analysis.model_config_version) {
      stale.value = true
      staleReason.value = '模型配置已变化,当前报告对应旧配置'
    } else {
      stale.value = false
      staleReason.value = ''
    }
    if (!disposed) void loadHistory()
  } catch (e) {
    if (axios.isCancel(e)) return // 主动取消,不是错误
    if (seq !== requestSeq || props.faultId !== faultId) return // 过期错误不打扰
    const status = axios.isAxiosError(e) ? e.response?.status : undefined
    if (status === 429) {
      rateLimited.value = true
      error.value = { message: errMsg(e) || '请求过于频繁,已暂停自动分析,请稍后手动重试', status }
    } else if (status === 400) {
      emptyHint.value = true
      error.value = { message: errMsg(e), status }
    } else {
      // 模型失败:保留输入与既有报告,可手动重试;不回退预设答案
      error.value = { message: errMsg(e), status }
    }
    // 请求失败但保留上一份成功报告时:明确标注旧结果,不把旧报告冒充本次结果
    markStale('本次分析未成功,以下为上一份结果(旧结果)')
  } finally {
    if (disposed) return // 卸载后不得再排队任务(取消请求会进入 finally)
    if (seq === requestSeq) {
      analyzing.value = false
      if (pendingAuto) {
        // 在途期间又有输入:重新排队分析最新输入(发送前 analyze 会再校验开关/限流/实质变化)
        pendingAuto = false
        clearTimeout(debounceTimer)
        debounceTimer = setTimeout(() => {
          if (!disposed) void analyze('auto')
        }, 300)
      }
    }
  }
}

function retry() {
  rateLimited.value = false
  void analyze('manual')
}

// ===== 输入停顿防抖 =====
watch(inputSig, (val, old) => {
  if (val === old) return
  markStale('输入已变更,当前报告对应较早输入')
  if (!autoAnalyze.value || aiDisabled.value || rateLimited.value) return
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    if (!disposed) void analyze('auto')
  }, 2000)
})

// 关闭自动分析:清除已排定的计时器与待排队请求(审查 P2 #8)
watch(autoAnalyze, (val) => {
  if (!val) {
    clearTimeout(debounceTimer)
    pendingAuto = false
  }
})

// 模型配置版本变化:旧报告标记过期(绑定配置版本)
watch(
  () => props.aiStatus?.model_config_version,
  (val) => {
    if (!val || !analysis.value) return
    if (analysis.value.model_config_version && analysis.value.model_config_version !== val) {
      markStale('模型配置已变化,当前报告对应旧配置')
    }
    // 配置版本变化后允许重新自动分析
    if (rateLimited.value) rateLimited.value = false
  },
)

// ===== 分析历史(倒序,最新在前)=====
// 请求序号/故障 ID 防护:切换故障后旧历史响应不得覆盖新故障历史
let historySeq = 0

async function loadHistory() {
  const faultId = props.faultId
  const seq = ++historySeq
  try {
    const { data } = await client.get<AiAnalysis[]>(`/api/faults/${faultId}/analyses`, {
      params: { limit: 20 },
    })
    if (seq !== historySeq || props.faultId !== faultId) return // 过期历史响应丢弃
    history.value = data
  } catch {
    /* 历史加载失败不影响分析主流程 */
  }
}

function viewHistoryItem(item: AiAnalysis) {
  analysis.value = item
  reused.value = false
  error.value = null
  // 历史报告对应当时的输入/配置快照,一律标记为非实时结果
  const cfgNow = props.aiStatus?.model_config_version ?? ''
  stale.value = true
  if (cfgNow && item.model_config_version && cfgNow !== item.model_config_version) {
    staleReason.value = '模型配置已变化,历史报告对应旧配置'
  } else {
    staleReason.value = '历史报告(对应较早输入,非实时结果)'
  }
}

// ===== 故障切换:中止在途请求并复位 =====
watch(
  () => props.faultId,
  () => {
    controller?.abort()
    requestSeq++
    historySeq++
    pendingAuto = false
    clearTimeout(debounceTimer)
    // 旧请求已作废(序号不匹配不会回收状态):这里复位,避免新故障的自动触发被当成在途
    analyzing.value = false
    analysis.value = null
    reused.value = false
    stale.value = false
    staleReason.value = ''
    error.value = null
    emptyHint.value = false
    lastAnalyzedSig = ''
    history.value = []
    // 切换故障使旧检索失效
    searchSeq++
    hits.value = []
    searchDone.value = false
    searching.value = false
    searchError.value = ''
    searchMode.value = 'keyword'
    searchDegraded.value = false
    searchDegradeReason.value = ''
    void loadHistory()
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  // 卸载:使在途请求与待排队任务整体失效,防止 finally/计时器再补发请求
  disposed = true
  pendingAuto = false
  controller?.abort()
  clearTimeout(debounceTimer)
  searchSeq++ // 旧检索响应随卸载整体失效
})

// ===== 知识检索(关键词;状态确认后可用时向量混合)=====
async function runSearch() {
  const q = searchQuery.value.trim()
  if (!q) return
  const seq = ++searchSeq
  lastSearchQuery = q
  searching.value = true
  searchError.value = ''
  // 新查询开始即清旧方式标签/旧降级横幅,失败后不残留
  searchDegraded.value = false
  searchDegradeReason.value = ''
  try {
    // AI 状态未知:走服务端纯关键词路径(零 embedding 外发)。
    // 未确认模式与出网提示前不发送查询文本;关键词基本能力不依赖模型可达。
    const keywordOnly = aiStatusMissing.value
    const { data } = await client.get<SearchResponse>('/api/search', {
      params: keywordOnly ? { q, keyword_only: true } : { q },
    })
    // 过期响应(旧查询后到/故障切换/卸载)不覆盖新结果
    if (seq !== searchSeq || disposed || q !== lastSearchQuery) return
    hits.value = data.hits
    searchMode.value = data.retrieval_mode || 'keyword'
    searchDegraded.value = Boolean(data.degraded)
    searchDegradeReason.value = data.degrade_reason || ''
    searchDone.value = true
  } catch (e) {
    if (seq !== searchSeq || disposed || q !== lastSearchQuery) return // 过期错误不打扰
    searchError.value = errMsg(e)
    // 失败后旧检索方式标签/降级横幅不再代表本次结果:清掉
    searchMode.value = 'keyword'
    searchDegraded.value = false
    searchDegradeReason.value = ''
  } finally {
    // 过期请求不修改 loading(由当前请求/状态复位负责)
    if (seq === searchSeq && !disposed) searching.value = false
  }
}

// ===== 展示辅助 =====
function md(text: string): string {
  return renderSafeMd(text ?? '')
}

function priorityTag(p: string): { label: string; type: 'danger' | 'warning' | 'info' } {
  if (p.includes('优先排查')) return { label: '优先排查', type: 'danger' }
  if (p.includes('次优先')) return { label: '次优先', type: 'warning' }
  return { label: p || '证据不足', type: 'info' }
}

function refLabel(refId: string): string {
  const [kind, id] = refId.split(':')
  return kind === 'sop' ? `SOP #${id}` : kind === 'case' ? `案例 #${id}` : refId
}

function suggestedCategoryId(): number | null {
  const name = analysis.value?.report?.suggested_category
  if (!name) return null
  const cat = props.categories.find((c) => c.name === name)
  return cat ? cat.id : null
}

function applySuggestion() {
  const rep = analysis.value?.report
  if (!rep) return
  emit('apply-suggestion', {
    category_id: suggestedCategoryId(),
    tags: [...(rep.suggested_tags ?? [])],
  })
  ElMessage.success('已填入建议分类/标签,请人工确认后保存')
}

async function copyCommand(cmd: string) {
  try {
    await navigator.clipboard.writeText(cmd)
    ElMessage.success('已复制命令(仅复制,系统不会执行)')
  } catch {
    ElMessage.warning('复制失败,请手动选择复制')
  }
}

const report = computed<AnalysisReport | null>(() => analysis.value?.report ?? null)

// ===== 业务失败展示=====
// HTTP 201 只代表创建了分析记录,不代表分析成功;业务 failed 与历史 failed
// 从分析记录统一派生(error_code/error_message),持久显示原因与人工重试入口,
// 不再要求「有 report」或「无 analysis」——failed/report=null 不得显示空白。
const analysisFailed = computed(() => analysis.value?.status === 'failed')
const failureReason = computed(() => {
  const a = analysis.value
  if (!a) return ''
  return a.error_message?.trim() || '分析失败(未记录原因)'
})

function usageText(a: AiAnalysis): string {
  const u = a.usage
  if (!u || Object.keys(u).length === 0) return '用量未知'
  // 按字段展示:后端只汇总每次调用都返回的字段,缺失字段显示「未知」,
  // 不把部分调用的值当作整次合计
  const fmt = (v: unknown) => (typeof v === 'number' ? String(v) : '未知')
  return `用量:输入 ${fmt(u.prompt_tokens)} / 输出 ${fmt(u.completion_tokens)} / 合计 ${fmt(u.total_tokens)}(仅接口返回时记录)`
}

function statusLabel(a: AiAnalysis): string {
  return a.status === 'success' ? '成功' : a.status === 'incomplete' ? '结果不完整' : '失败'
}

function triggerLabel(t: string): string {
  return t === 'auto' ? '自动' : '手动'
}
</script>

<template>
  <aside class="assistant-panel" aria-label="AI 排查助手">
    <div class="ops-panel assistant-shell">
      <!-- 头部:标题 + 模式 -->
      <div class="assistant-heading">
        <div class="assistant-heading-label"><span class="assistant-brand-icon"><AppIcon name="bot" :size="21" /></span><div><h2>AI 排查助手</h2><p>证据驱动的排查建议</p></div></div>
        <div class="assistant-heading-actions">
          <el-tag v-if="aiStatus" size="small" :type="aiDisabled ? 'info' : aiStatus.ai_mode === 'demo' ? 'warning' : 'primary'">
            {{ aiStatus.ai_mode === 'demo' ? '演示模式' : aiDisabled ? '未启用' : aiStatus.ai_mode === 'local' ? '本地模型' : userProviderLabel ? `${userProviderLabel} 模型` : '云端模型' }}
          </el-tag>
          <el-button size="small" plain @click="openSettings">模型设置</el-button>
        </div>
      </div>

      <!-- 模式与出网提示 -->
      <div
        v-if="aiStatus"
        class="assistant-mode-note ops-note"
        :class="aiDisabled ? 'is-muted' : 'is-warning'"
      >
        {{ aiStatus.message }}
      </div>
      <!-- 状态未知:暂停模型调用并提供重试 -->
      <div
        v-else
        class="assistant-mode-note ops-note is-warning"
      >
        AI 状态未知:已暂停自动分析与模型调用(未确认模式与出网提示前不发送);知识检索仅走关键词路径,不外发查询文本。
        <el-button size="small" type="warning" plain class="ml-1" @click="emit('retry-status')">
          重新加载状态
        </el-button>
      </div>

      <!-- 操作栏 -->
      <div class="assistant-toolbar">
        <el-button
          type="primary"
          size="small"
          :loading="analyzing"
          :disabled="aiDisabled"
          @click="analyze('manual')"
        >
          立即分析
        </el-button>
        <el-switch v-model="autoAnalyze" size="small" active-text="自动分析" :disabled="aiDisabled" />
        <span class="assistant-trigger-hint">输入停顿 2 秒后触发</span>
      </div>

      <!-- 状态与提示区 -->
      <div class="assistant-request-status space-y-1.5">
        <div v-if="analyzing" class="ops-note is-info">
          分析中(检索知识 → 生成报告)…
        </div>
        <el-alert
          v-if="emptyHint"
          type="info"
          :closable="true"
          show-icon
          title="输入为空或无实质内容,未触发分析"
          @close="emptyHint = false"
        />
        <el-alert
          v-if="stale && staleReason"
          type="warning"
          :closable="false"
          show-icon
          :title="staleReason"
        />
        <div
          v-if="error"
          class="ops-note is-danger"
        >
          <div class="font-medium mb-1">分析失败:{{ error.message }}</div>
          <div class="flex items-center gap-2">
            <el-button size="small" type="danger" plain @click="retry">重试</el-button>
            <span v-if="rateLimited" class="ops-note-detail">已暂停自动分析,请稍后手动重试</span>
            <span v-else class="ops-note-detail">输入与记录不受影响,可继续编辑后重试</span>
          </div>
        </div>
      </div>

      <!-- 报告主体(独立滚动) -->
      <div class="assistant-report">
        <!-- 演示数据标注 -->
        <div
          v-if="analysis && isDemo"
          class="ops-note is-demo"
        >
          <b>演示数据 / 模拟分析</b>:以下结果由演示适配器模拟生成,不代表真实模型能力。
        </div>

        <!-- 分析失败(业务 failed:新结果与历史统一,持久显示原因与人工重试入口) -->
        <div
          v-if="analysisFailed"
          class="ops-note is-danger"
        >
          <div class="font-medium mb-1">分析失败:{{ failureReason }}</div>
          <div v-if="analysis?.error_code" class="ops-note-detail mb-1">
            失败类型:{{ analysis.error_code }}
          </div>
          <div class="flex items-center gap-2">
            <el-button size="small" type="danger" plain @click="retry">重试</el-button>
            <span class="ops-note-detail">输入与记录不受影响,可继续编辑后重试</span>
          </div>
        </div>

        <div v-if="analysis && report" class="assistant-report-body ops-result-in" :key="analysis.id">
          <!-- 摘要 -->
          <section>
            <div class="flex items-center gap-2 mb-1">
              <h3 class="assistant-section-title">摘要</h3>
              <el-tag v-if="isDemo" size="small" type="warning" effect="plain">演示数据 / 模拟分析</el-tag>
            </div>
            <div class="assistant-body md-body" v-html="md(report.summary)"></div>
          </section>

          <!-- 已知事实 -->
          <section v-if="report.observed_facts.length">
            <h3 class="assistant-section-title mb-1">已知事实</h3>
            <ul class="space-y-1.5">
              <li v-for="(f, i) in report.observed_facts" :key="i" class="observed-fact">
                <div class="md-body" v-html="md(f.fact)"></div>
                <div v-if="f.evidence_ids?.length" class="flex flex-wrap gap-1 mt-0.5">
                  <el-tag v-for="eid in f.evidence_ids" :key="eid" size="small" type="info" effect="plain">
                    {{ eid }}
                  </el-tag>
                </div>
              </li>
            </ul>
          </section>

          <!-- 建议分类/标签(人工确认后才生效) -->
          <section v-if="report.suggested_category || report.suggested_tags.length">
            <h3 class="assistant-section-title mb-1">建议分类与标签</h3>
            <div class="flex flex-wrap items-center gap-1.5">
              <el-tag v-if="report.suggested_category" size="small">分类:{{ report.suggested_category }}</el-tag>
              <el-tag v-for="t in report.suggested_tags" :key="t" size="small" type="info">{{ t }}</el-tag>
              <el-button v-if="canEdit" size="small" plain @click="applySuggestion">采纳到表单</el-button>
            </div>
            <div class="assistant-hint mt-1">采纳后仍可人工修改;AI 不会自动保存或改写表单。</div>
          </section>

          <!-- 优先排查方向 / 候选原因 -->
          <section v-if="report.hypotheses.length">
            <div class="flex items-center gap-2 mb-1">
              <h3 class="assistant-section-title">候选原因与排查方向</h3>
              <el-tag v-if="isDemo" size="small" type="warning" effect="plain">演示数据 / 模拟分析</el-tag>
            </div>
            <div class="space-y-2">
              <div
                v-for="(h, i) in report.hypotheses"
                :key="i"
                class="hypothesis-card"
              >
                <div class="flex items-start justify-between gap-2 mb-1">
                  <div class="hypothesis-title"><span class="hypothesis-number">{{ String(i + 1).padStart(2, '0') }}</span><div class="hypothesis-title-text md-body" v-html="md(h.title)"></div></div>
                  <el-tag size="small" :type="priorityTag(h.priority).type" effect="plain">
                    {{ priorityTag(h.priority).label }}
                  </el-tag>
                </div>
                <div class="hypothesis-body">
                  <div><span class="hypothesis-label">依据:</span><span class="md-body" v-html="md(h.rationale)"></span></div>
                  <div v-if="h.missing_information?.length">
                    <span class="hypothesis-label">待补信息:</span>{{ h.missing_information.join('；') }}
                  </div>
                  <div v-if="h.verification">
                    <span class="hypothesis-label">验证方法:</span><span class="md-body" v-html="md(h.verification)"></span>
                  </div>
                  <div v-if="h.evidence_ids?.length || h.reference_ids?.length" class="flex flex-wrap gap-1 mt-1">
                    <el-tag v-for="eid in h.evidence_ids" :key="'e' + eid" size="small" type="info" effect="plain">{{ eid }}</el-tag>
                    <el-tag v-for="rid in h.reference_ids" :key="'r' + rid" size="small" type="success" effect="plain">{{ refLabel(rid) }}</el-tag>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- 待补信息 -->
          <section v-if="report.missing_information.length">
            <h3 class="assistant-section-title mb-1">待补信息</h3>
            <ul class="assistant-hint list-disc pl-5">
              <li v-for="(m, i) in report.missing_information" :key="i" class="md-body" v-html="md(m)"></li>
            </ul>
          </section>

          <!-- 下一步检查(仅展示与复制) -->
          <section v-if="report.next_checks.length">
            <h3 class="assistant-section-title mb-1">下一步检查</h3>
            <div class="space-y-2">
              <div v-for="(c, i) in report.next_checks" :key="i" class="next-check-card">
                <div class="assistant-hint md-body" v-html="md(c.description)"></div>
                <div v-if="c.command" class="mt-1 flex items-start gap-1.5">
                  <code class="assistant-command">{{ c.command }}</code>
                  <el-button size="small" plain @click="copyCommand(c.command)">复制</el-button>
                </div>
                <div v-if="c.related_hypothesis" class="assistant-hint mt-1">
                  关联:{{ c.related_hypothesis }}
                </div>
              </div>
            </div>
            <div class="assistant-hint mt-1">命令仅展示与复制,系统不会自动执行;执行前请自行确认适用前提与影响。</div>
          </section>

          <!-- 相关案例和 SOP -->
          <section v-if="report.references.length">
            <h3 class="assistant-section-title mb-1">相关案例和 SOP</h3>
            <ul class="space-y-1">
              <li v-for="r in report.references" :key="r.ref_id" class="reference-card">
                <el-tag size="small" :type="r.source_type === 'sop' ? 'primary' : 'success'" effect="plain">
                  {{ refLabel(r.ref_id) }}
                </el-tag>
                <span v-if="sourceVersionText(r.version)" class="assistant-hint ml-1">
                  {{ sourceVersionText(r.version) }}
                </span>
                <span class="ml-1 md-body" v-html="md(r.relevance)"></span>
                <!-- 来源地址只由受控 source_type/source_id 构造,不使用模型返回的任意 URL -->
                <a
                  v-if="sourcePath(r.source_type, r.source_id)"
                  :href="sourcePath(r.source_type, r.source_id) || undefined"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ops-link ml-1.5 whitespace-nowrap"
                  :title="`${sourceViewLabel(r.source_type)}(在新标签页打开,不影响当前未保存输入)`"
                  data-testid="reference-source-link"
                >
                  {{ sourceViewLabel(r.source_type) }} ↗
                </a>
                <span v-else class="assistant-hint ml-1.5">来源无效</span>
              </li>
            </ul>
          </section>

          <!-- 证据不足 / 不完整说明 -->
          <el-alert
            v-if="report.insufficient_evidence"
            type="info"
            :closable="false"
            show-icon
            title="证据不足:以上方向仅供参考,请先补充检查结果"
          />
          <div v-if="analysis.incomplete_reasons?.length" class="ops-note is-warning">
            <div class="font-medium mb-0.5">结果不完整:</div>
            <ul class="list-disc pl-4">
              <li v-for="(r, i) in analysis.incomplete_reasons" :key="i">{{ r }}</li>
            </ul>
          </div>

        </div>

        <!-- 分析元信息:独立于报告展示(失败/report=null 时同样可见;无 usage 显示未知) -->
        <section
          v-if="analysis"
          class="assistant-meta"
        >
          <div>分析时间:{{ fmtTime(analysis.created_at) }}</div>
          <div>
            状态:{{ statusLabel(analysis) }} · 触发:{{ triggerLabel(analysis.trigger_type) }} ·
            调用次数:{{ analysis.call_count }} · 延迟:{{ analysis.latency_ms }}ms
            <el-tag v-if="reused" size="small" type="info" effect="plain" class="ml-1">复用缓存结果</el-tag>
          </div>
          <div>{{ usageText(analysis) }}</div>
          <div>
            输入版本:{{ analysis.input_hash.slice(0, 8) }} · 配置版本:{{ analysis.model_config_version || '未知' }} ·
            知识版本:{{ analysis.knowledge_version || '未知' }}
          </div>
        </section>

        <!-- 请求层失败且无分析记录 -->
        <div v-else-if="error" class="assistant-hint">
          本次分析失败,上方可重试。左侧输入不会丢失。
        </div>

        <!-- 空状态 -->
        <div v-else class="assistant-empty">
          <span class="assistant-empty-icon"><AppIcon name="files" :size="27" /></span>
          <div class="assistant-empty-eyebrow">START WITH EVIDENCE</div>
          <h3>从现场证据开始</h3>
          <p class="mb-2">尚无分析结果。在左侧补充问题描述、报错日志后:</p>
          <ul class="list-disc pl-5 space-y-1">
            <li>输入停顿约 2 秒自动分析(可在上方关闭)</li>
            <li>或点击「立即分析」手动触发</li>
            <li>也可先在下方用关键词检索相关案例与 SOP</li>
          </ul>
        </div>

        <!-- 分析历史 -->
        <section v-if="history.length" class="assistant-section">
          <div
            class="flex items-center justify-between cursor-pointer select-none"
            role="button"
            tabindex="0"
            @keydown.enter="historyOpen = !historyOpen"
            @click="historyOpen = !historyOpen"
          >
            <h3 class="assistant-section-title">分析历史({{ history.length }})</h3>
            <span class="assistant-hint">{{ historyOpen ? '收起' : '展开' }}</span>
          </div>
          <div v-if="historyOpen" class="mt-1.5 space-y-1">
            <div
              v-for="h in history"
              :key="h.id"
              class="history-row"
              :class="{ 'is-active': analysis?.id === h.id }"
              @click="viewHistoryItem(h)"
            >
              <div class="history-row-meta">
                {{ fmtTime(h.created_at) }}
                <el-tag size="small" :type="h.status === 'success' ? 'success' : h.status === 'incomplete' ? 'warning' : 'danger'" effect="plain">
                  {{ statusLabel(h) }}
                </el-tag>
                <el-tag v-if="h.is_demo" size="small" type="warning" effect="plain">演示</el-tag>
              </div>
              <span class="history-row-trigger">{{ triggerLabel(h.trigger_type) }}</span>
            </div>
            <div class="assistant-hint">历史按时间倒序返回;点击可查看,历史报告可能对应较早输入。</div>
          </div>
        </section>

        <!-- 知识检索(关键词 + 可用时向量混合;实际方式与降级如实标注) -->
        <section class="assistant-section">
          <div class="flex items-center gap-2 mb-1.5">
            <h3 class="assistant-section-title">知识检索</h3>
            <el-tag v-if="searchDone" size="small" :type="searchMode === 'hybrid' ? 'primary' : 'info'" effect="plain">
              {{ searchMode === 'hybrid' ? '混合检索(关键词+向量)' : '关键词检索' }}
            </el-tag>
          </div>
          <div class="flex gap-1.5">
            <el-input
              v-model="searchQuery"
              size="small"
              placeholder="错误码 / 组件 / 现象关键词"
              clearable
              @keyup.enter="runSearch"
            />
            <el-button size="small" :loading="searching" @click="runSearch">检索</el-button>
          </div>
          <div v-if="searchDone && searchDegraded" class="ops-note is-warning mt-1.5">
            已降级为关键词检索:{{ searchDegradeReason || '向量路径不可用' }}
          </div>
          <div v-if="searchDone && aiStatusMissing" class="assistant-hint is-warning mt-1">
            AI 状态未知:本次仅关键词检索(纯关键词路径,查询文本未发送至 embedding 服务);状态恢复后可用混合检索。
          </div>
          <div v-else-if="aiStatus?.embedding_mode === 'cloud'" class="assistant-hint is-warning mt-1">
            说明:查询文本(脱敏后)与索引构建时的脱敏知识内容会发送至已配置的 embedding 服务。
          </div>
          <div v-else-if="aiStatus?.embedding_mode === 'local'" class="assistant-hint mt-1">
            向量检索使用本地/内网 embedding 服务(不外发云端)。
          </div>
          <div v-if="searchError" class="assistant-hint is-danger mt-1">检索失败:{{ searchError }}</div>
          <div v-else-if="searchDone && !hits.length" class="assistant-hint mt-1">
            无匹配的正式知识(已审核案例与有效已发布 SOP);检索仅作推荐,不自动确认根因。
          </div>
          <div v-else-if="hits.length" class="mt-1.5 space-y-1.5">
            <div
              v-for="h in hits"
              :key="h.ref_id"
              class="knowledge-hit"
            >
              <div class="flex items-center gap-1.5">
                <el-tag size="small" :type="h.source_type === 'sop' ? 'primary' : 'success'" effect="plain">
                  {{ sourceTagLabel(h.source_type) }}
                </el-tag>
                <span class="knowledge-hit-title">{{ h.title }}</span>
                <span v-if="sourceVersionText(h.version)" class="assistant-hint">
                  {{ sourceVersionText(h.version) }}
                </span>
                <!-- 案例与 SOP 均提供明确入口;新标签页打开,保留左侧未保存输入 -->
                <a
                  v-if="sourcePath(h.source_type, h.source_id)"
                  :href="sourcePath(h.source_type, h.source_id) || undefined"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ops-link ml-auto whitespace-nowrap"
                  :title="`${sourceViewLabel(h.source_type)}(在新标签页打开,不影响当前未保存输入)`"
                  data-testid="search-hit-source-link"
                >
                  {{ sourceViewLabel(h.source_type) }} ↗
                </a>
                <span v-else class="assistant-hint ml-auto">来源无效</span>
              </div>
              <div class="knowledge-hit-snippet mt-0.5">{{ h.snippet }}</div>
            </div>
            <div class="assistant-hint">
              仅检索已审核案例与当前有效已发布 SOP;查看来源会在新标签页打开,不影响当前未保存输入。
            </div>
          </div>
        </section>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.assistant-panel { display: flex; flex-direction: column; width: 100%; height: 100%; min-width: 0; min-height: 0; }
.assistant-shell { display: flex; flex: 1; flex-direction: column; min-height: 0; padding: 20px; overflow: hidden; border-color: var(--ops-border); background: var(--ops-surface); }
.assistant-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 1px 0 15px; }
.assistant-heading-label { display: flex; align-items: center; gap: 10px; }
.assistant-brand-icon { display: grid; place-items: center; width: 36px; height: 36px; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }
.assistant-heading h2 { margin: 0; color: var(--ops-text); font-size: 15px; font-weight: 650; }
.assistant-heading p { margin: 3px 0 0; color: var(--ops-muted); font-size: 11px; }
.assistant-heading :deep(.el-tag) { flex-shrink: 0; font-size: 10px; }
.assistant-mode-note { flex-shrink: 0; margin-bottom: 14px; font-size: 11px; line-height: 1.8; }
.assistant-toolbar { display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 12px; row-gap: 8px; align-items: center; padding-bottom: 16px; border-bottom: 1px solid var(--ops-border-light); flex-shrink: 0; }
.assistant-heading-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.assistant-toolbar > :deep(.el-button) { justify-self: start; min-width: 106px; height: 32px; font-weight: 600; }
.assistant-toolbar > :deep(.el-switch) { justify-self: end; }
.assistant-toolbar :deep(.el-switch__label) { color: var(--ops-muted); font-size: 11px; }
.assistant-trigger-hint { grid-column: 1 / -1; color: var(--ops-faint); font-size: 10px; letter-spacing: 0.1px; }
.assistant-request-status { flex-shrink: 0; }
.assistant-request-status > :first-child { margin-top: 12px; }
.assistant-report { flex: 1; min-height: 0; margin-right: -5px; padding-right: 5px; overflow: auto; }
.assistant-report-body > div:first-child { margin-top: 14px; }
.assistant-report-body > section, .assistant-report > section { padding: 18px 0; border-bottom: 1px solid var(--ops-border-light); }
.assistant-report-body > section:last-child, .assistant-report > section:last-child { padding-bottom: 4px; border-bottom: 0; }
.assistant-section { margin-top: 4px; padding-top: 14px; border-top: 1px solid var(--ops-border-light); }
.assistant-section-title { margin: 0 0 10px; color: var(--ops-text); font-size: 12px; font-weight: 650; letter-spacing: 0.01em; }
.assistant-body { color: var(--ops-text-soft); font-size: 12px; line-height: 1.9; }
.assistant-hint { color: var(--ops-muted); font-size: 11px; line-height: 1.8; }
.assistant-hint.is-warning { color: #7a5410; }
.assistant-hint.is-danger { color: var(--ops-danger); }
.assistant-meta { margin-top: 10px; padding-top: 12px; border-top: 1px solid var(--ops-border-light); color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.9; }
.assistant-report :deep(.el-tag) { font-size: 10px; }
.assistant-report .md-body { overflow-wrap: anywhere; }
.observed-fact { padding: 9px 11px; border-left: 2px solid var(--ops-border-strong); border-radius: 3px; background: var(--ops-surface-muted); }
.observed-fact + .observed-fact { margin-top: 8px; }
.hypothesis-card { padding: 13px; border: 1px solid var(--ops-border-light); border-radius: var(--ops-radius-panel); background: var(--ops-surface); }
.hypothesis-title { display: flex; align-items: flex-start; gap: 8px; }
.hypothesis-title-text { color: var(--ops-text); font-size: 12px; font-weight: 600; line-height: 1.7; }
.hypothesis-number { display: grid; place-items: center; width: 22px; height: 22px; flex-shrink: 0; border-radius: 4px; background: var(--ops-brand-soft); color: var(--ops-brand); font-family: var(--ops-font-mono); font-size: 10px; }
.hypothesis-card > div:last-child { margin-top: 9px; }
.hypothesis-body { color: var(--ops-text-soft); font-size: 11px; line-height: 1.85; }
.hypothesis-label { color: var(--ops-muted); }
.next-check-card { padding: 12px; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); }
.assistant-command { flex: 1; min-width: 0; padding: 10px; border-radius: var(--ops-radius-control); background: var(--ops-code-surface); color: var(--ops-code-text); font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.reference-card { padding: 11px; border: 1px solid var(--ops-border-light); border-radius: var(--ops-radius-panel); background: var(--ops-surface-muted); font-size: 11px; line-height: 1.8; }
.reference-card + .reference-card { margin-top: 8px; }
.assistant-empty { padding: 30px 6px 26px; }
.assistant-empty-icon { display: grid; place-items: center; width: 56px; height: 56px; margin: 0 auto 17px; border: 1px solid var(--ops-border-light); border-radius: var(--ops-radius-panel); background: var(--ops-surface-warm); color: var(--ops-brand); }
.assistant-empty-eyebrow { color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 9px; font-weight: 600; letter-spacing: 1.6px; text-align: center; }
.assistant-empty h3 { margin: 7px 0 18px; color: var(--ops-text); font-size: 15px; font-weight: 600; text-align: center; }
.assistant-empty p, .assistant-empty li { color: var(--ops-muted); font-size: 11px; line-height: 1.9; }
.knowledge-hit { padding: 12px; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); transition: border-color var(--ops-duration-fast) var(--ops-ease-standard), background-color var(--ops-duration-fast) var(--ops-ease-standard); }
.knowledge-hit:hover { border-color: var(--ops-border-strong); background: var(--ops-surface-muted); }
.knowledge-hit > div:first-child { flex-wrap: wrap; }
.knowledge-hit-title { color: var(--ops-text); font-size: 12px; font-weight: 600; }
.knowledge-hit-snippet { color: var(--ops-muted); font-size: 11px; line-height: 1.8; }
.history-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 7px 8px; border-radius: var(--ops-radius-control); font-size: 11px; cursor: pointer; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard); }
.history-row:hover { background: var(--ops-surface-muted); }
.history-row.is-active { background: var(--ops-brand-soft); }
.history-row-meta { color: var(--ops-text-soft); font-family: var(--ops-font-mono); font-size: 10px; }
.history-row-trigger { color: var(--ops-muted); font-size: 10px; }
.md-body :deep(code.md-inline) { padding: 1px 4px; border-radius: 3px; background: var(--ops-surface-warm); color: #7a4a22; font-family: var(--ops-font-mono); font-size: 0.92em; }
.md-body :deep(pre.md-code) { margin: 8px 0; padding: 11px 12px; overflow-x: auto; border-radius: var(--ops-radius-control); background: var(--ops-code-surface); color: var(--ops-code-text); font-size: 11px; line-height: 1.8; }
.md-body :deep(pre.md-code code) { padding: 0; background: transparent; color: inherit; }
.md-body :deep(strong) { color: var(--ops-text); }
@media (max-width: 1180px) {
  .assistant-shell { padding: 17px; }
  .assistant-heading-label { gap: 8px; }
  .assistant-brand-icon { width: 33px; height: 33px; }
  .assistant-heading h2 { font-size: 14px; }
  .assistant-toolbar { column-gap: 8px; }
  .assistant-toolbar > :deep(.el-button) { min-width: 95px; }
}
@media (max-width: 1020px) {
  .assistant-panel { height: auto; min-height: 640px; }
  .assistant-report { max-height: 820px; }
}
</style>



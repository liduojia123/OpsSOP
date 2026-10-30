<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type {
  Category,
  FeedbackKind,
  Sop,
  SopDetail,
  SopFeedback,
  SopStatus,
  UserOption,
} from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sop = ref<SopDetail | null>(null)
const history = ref<Sop[]>([])
const feedbacks = ref<SopFeedback[]>([])
const categories = ref<Category[]>([])
const userOptions = ref<UserOption[]>([])
const saving = ref(false)
const submitting = ref(false)
const loading = ref(false)
const loadError = ref<{ kind: 'invalid' | 'forbidden' | 'not-found' | 'network' | 'server'; message: string } | null>(null)
// 请求序号:切换版本时丢弃过期响应,防止旧内容覆盖新页面
let loadSeq = 0

function parseSopId(raw: unknown): number | null {
  if (typeof raw !== 'string' || !/^\d+$/.test(raw)) return null
  const id = Number(raw)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

const form = reactive({
  title: '',
  category_id: null as number | null,
  tags: [] as string[],
  applicable_env: '',
  symptom: '',
  preconditions: '',
  check_steps: '',
  handling_steps: '',
  recovery_verification: '',
  rollback_steps: '',
  notes: '',
  maintainer_id: null as number | null,
})

const reviewComment = ref('')
const feedbackDialog = ref(false)
const feedbackForm = reactive({ kind: 'effective' as FeedbackKind, comment: '' })
const feedbackSubmitting = ref(false)

const canEdit = computed(
  () =>
    sop.value &&
    ['draft', 'returned'].includes(sop.value.status) &&
    (auth.role === 'admin' || sop.value.created_by === auth.user?.id),
)
const canReview = computed(() => auth.role === 'reviewer' || auth.role === 'admin')

function fillForm(data: Sop) {
  form.title = data.title
  form.category_id = data.category_id
  form.tags = [...(data.tags ?? [])]
  form.applicable_env = data.applicable_env
  form.symptom = data.symptom
  form.preconditions = data.preconditions
  form.check_steps = data.check_steps
  form.handling_steps = data.handling_steps
  form.recovery_verification = data.recovery_verification
  form.rollback_steps = data.rollback_steps
  form.notes = data.notes
  form.maintainer_id = data.maintainer_id
}

async function load() {
  const id = parseSopId(route.params.id)
  const seq = ++loadSeq
  // 清除旧版本状态,避免 URL 与页面内容不一致;失败时不得残留上一个来源正文
  sop.value = null
  history.value = []
  feedbacks.value = []
  reviewComment.value = ''
  loadError.value = null
  if (id === null) {
    loading.value = false
    loadError.value = { kind: 'invalid', message: '来源地址无效:SOP ID 必须是正整数' }
    return
  }
  loading.value = true
  try {
    const [detail, hist, fb] = await Promise.all([
      client.get(`/api/sops/${id}`),
      client.get(`/api/sops/${id}/history`),
      client.get(`/api/sops/${id}/feedback`),
    ])
    if (seq !== loadSeq) return // 过期响应丢弃
    sop.value = detail.data
    history.value = hist.data
    feedbacks.value = fb.data
    fillForm(detail.data)
  } catch (e) {
    if (seq !== loadSeq) return
    const status = axios.isAxiosError(e) ? e.response?.status : undefined
    if (status === 403) {
      loadError.value = { kind: 'forbidden', message: '当前账号无权查看该 SOP' }
    } else if (status === 404) {
      loadError.value = { kind: 'not-found', message: 'SOP 不存在或已被删除' }
    } else if (status === 400 || status === 422) {
      loadError.value = { kind: 'invalid', message: 'SOP ID 无效' }
    } else if (!axios.isAxiosError(e) || !e.response) {
      loadError.value = { kind: 'network', message: `网络错误:${errMsg(e)}` }
    } else {
      loadError.value = { kind: 'server', message: errMsg(e) || '加载 SOP 失败' }
    }
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

// 同组件内切换历史版本:监听路由参数并按新 ID 加载
watch(
  () => route.params.id,
  () => {
    load()
  },
)

function formPayload() {
  return { ...form, tags: [...form.tags] }
}

// 快照:请求期间用户切换历史版本时,多步请求仍作用于发起时的记录 ID
function currentSopId(): number | null {
  return sop.value?.id ?? null
}

async function save() {
  if (saving.value) return false
  const id = currentSopId()
  const expected = sop.value?.updated_at
  if (id == null || !expected) return false
  saving.value = true
  try {
    // 只回写服务端元数据,不回填表单:请求期间继续输入的内容不被响应覆盖
    const { data } = await client.patch(`/api/sops/${id}`, formPayload(), { headers: { 'X-Expected-Updated-At': expected } })
    if (sop.value && sop.value.id === id) {
      sop.value.updated_at = data.updated_at
      sop.value.status = data.status
    }
    ElMessage.success('已保存')
    return true
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留编辑
    return false
  } finally {
    saving.value = false
  }
}

// 提交审核前先保存表单并等待成功,确保审核针对当前编辑内容;失败保留编辑
async function submitForReview() {
  if (submitting.value) return
  const id = currentSopId()
  const expected = sop.value?.updated_at
  if (id == null || !expected) return
  submitting.value = true
  try {
    // 多步提交使用发起时的 ID 快照;等待期间切换历史版本不会改变请求目标
    const { data } = await client.patch(`/api/sops/${id}`, formPayload(), { headers: { 'X-Expected-Updated-At': expected } })
    if (sop.value?.id === id) sop.value.updated_at = data.updated_at
    await client.post(`/api/sops/${id}/submit`, { comment: '' }, { headers: { 'X-Expected-Updated-At': data.updated_at } })
    ElMessage.success('已提交审核')
    // 仅当仍停留在发起提交的记录上时才刷新,避免覆盖切换后的页面
    if (Number(route.params.id) === id) {
      await load()
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    submitting.value = false
  }
}

// 审核/废弃等动作作用于发起时的记录快照
async function action(path: string, comment?: string) {
  const id = currentSopId()
  const expected = sop.value?.updated_at
  if (id == null || !expected) return
  try {
    await client.post(`/api/sops/${id}/${path}`, { comment: comment ?? '' }, { headers: { 'X-Expected-Updated-At': expected } })
    ElMessage.success('操作成功')
    if (Number(route.params.id) === id) {
      await load()
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function approve() {
  await action('approve', reviewComment.value)
}

async function doReturn() {
  if (!reviewComment.value.trim()) {
    ElMessage.warning('退回必须填写审核意见')
    return
  }
  await action('return', reviewComment.value)
}

async function deprecate() {
  await ElMessageBox.confirm('确认废弃该 SOP?废弃后不再作为有效知识。', '废弃确认', {
    type: 'warning',
  }).catch(() => {
    throw new Error('cancel')
  })
  await action('deprecate', reviewComment.value)
}

async function newVersion() {
  const id = currentSopId()
  const expected = sop.value?.updated_at
  if (id == null || !expected) return
  try {
    const { data } = await client.post(`/api/sops/${id}/new-version`, undefined, { headers: { 'X-Expected-Updated-At': expected } })
    ElMessage.success(`已创建新草稿版本 v${data.version}(旧版本继续有效)`)
    // 等待导航完成;watcher 按新 ID 加载,避免读到旧路由参数
    await router.push({ name: 'sop-detail', params: { id: data.id } })
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function submitFeedback() {
  const id = currentSopId()
  if (id == null) return
  feedbackSubmitting.value = true
  try {
    const { data } = await client.post(`/api/sops/${id}/feedback`, feedbackForm)
    ElMessage.success('感谢反馈')
    feedbackDialog.value = false
    feedbackForm.comment = ''
    // 只追加反馈列表,不整页 reload,避免覆盖未保存的表单编辑
    if (sop.value && sop.value.id === id) {
      feedbacks.value.push(data)
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    feedbackSubmitting.value = false
  }
}

const feedbackKindMap: Record<FeedbackKind, string> = {
  effective: '有效',
  ineffective: '无效',
  suspected_outdated: '疑似过期',
}

function maintainerLabel(id: number | null) {
  if (id == null) return '-'
  return userOptions.value.find((u) => u.id === id)?.display_name ?? `用户 #${id}`
}

onMounted(async () => {
  try {
    const [cats, users] = await Promise.all([
      client.get('/api/categories'),
      client.get('/api/users/options'),
    ])
    categories.value = cats.data
    userOptions.value = users.data
  } catch {
    /* 下拉数据失败不阻塞详情加载 */
  }
  await load()
})
</script>

<template>
  <div v-if="!sop" class="ops-page sop-detail-page">
    <div v-if="loading" class="ops-text-muted">加载中…</div>
    <div v-else class="page-card max-w-3xl" data-testid="sop-detail-error">
      <el-alert
        :type="loadError?.kind === 'forbidden' || loadError?.kind === 'not-found' || loadError?.kind === 'invalid' ? 'warning' : 'error'"
        :closable="false"
        show-icon
        :title="loadError?.message || '无法加载该 SOP'"
      />
      <div class="mt-4 flex gap-2">
        <el-button
          v-if="loadError?.kind === 'network' || loadError?.kind === 'server'"
          type="primary"
          plain
          @click="load"
        >
          重试
        </el-button>
        <el-button @click="router.push('/sops')">返回 SOP 列表</el-button>
      </div>
    </div>
  </div>
  <div class="ops-page sop-detail-page" v-else>
    <div class="ops-page-header sop-page-header">
      <div class="sop-page-heading">
        <el-button text class="ops-back-link" @click="router.push('/sops')">← 返回 SOP 列表</el-button>
        <div class="ops-eyebrow">STANDARD OPERATING PROCEDURE</div>
        <h1>{{ sop.title }}</h1>
        <div class="sop-title-tags">
        <el-tag size="small" effect="plain">v{{ sop.version }}</el-tag>
        <el-tag size="small" :type="(sopStatusMap[sop.status as SopStatus].type as any)">
          {{ sopStatusMap[sop.status as SopStatus].label }}
        </el-tag>
        <span class="sop-title-mode">{{ canEdit ? '编辑草稿 · 提交后进入审核流程' : '只读文档 · 保留完整版本记录' }}</span>
        </div>
      </div>
      <div class="ops-page-actions sop-page-actions">
        <el-button v-if="canEdit" type="primary" :loading="saving" :disabled="submitting" @click="save">保存</el-button>
        <el-button v-if="canEdit" :loading="submitting" :disabled="saving" @click="submitForReview">提交审核</el-button>
        <el-button v-if="canReview && sop.status === 'submitted'" type="success" @click="approve">
          发布
        </el-button>
        <el-button v-if="canReview && sop.status === 'submitted'" type="danger" @click="doReturn">
          退回
        </el-button>
        <el-button
          v-if="canReview && ['published', 'superseded'].includes(sop.status)"
          type="danger"
          plain
          @click="deprecate"
        >
          废弃
        </el-button>
        <el-button
          v-if="['published', 'superseded'].includes(sop.status) && auth.role !== 'reviewer'"
          type="warning"
          plain
          @click="newVersion"
        >
          修改(生成新版本)
        </el-button>
        <el-button v-if="sop.status === 'published'" plain @click="feedbackDialog = true">
          反馈
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="sop.status === 'published'"
      type="success"
      :closable="false"
      title="当前有效版本(已发布)"
      class="mb-4"
    />
    <el-alert
      v-if="sop.status === 'superseded'"
      type="info"
      :closable="false"
      title="历史版本(已有新版本发布,本版本仅作存档)"
      class="mb-4"
    />
    <el-alert
      v-if="sop.status === 'deprecated'"
      type="error"
      :closable="false"
      title="已废弃,不再作为有效知识"
      class="mb-4"
    />
    <el-alert
      v-if="sop.status === 'returned'"
      type="error"
      :closable="false"
      :title="`已退回,审核意见: ${sop.review_comment}`"
      class="mb-4"
    />

    <div class="sop-workspace ops-result-in">
      <div class="ops-panel sop-document-panel">
        <!-- 保存/提交期间锁定编辑(审查 P2 #6):防止请求期间的新输入被提交后的旧快照覆盖 -->
        <el-form v-show="canEdit" label-position="top" class="sop-edit-form" :disabled="!canEdit || saving || submitting">
          <div class="sop-editor-heading"><span class="section-icon"><AppIcon name="edit" :size="18" /></span><div><h2>编写 SOP</h2><p>补全适用范围与操作步骤，再提交审核。</p></div></div>
          <el-form-item label="标题">
            <el-input v-model="form.title" />
          </el-form-item>
          <div class="sop-properties-grid">
            <el-form-item label="分类">
              <el-select v-model="form.category_id" clearable class="w-full">
                <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="维护人">
              <el-select v-model="form.maintainer_id" clearable class="w-full">
                <el-option
                  v-for="u in userOptions"
                  :key="u.id"
                  :label="`${u.display_name}(${u.username})`"
                  :value="u.id"
                />
              </el-select>
            </el-form-item>
          </div>
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
          <el-form-item label="适用环境及版本">
            <el-input v-model="form.applicable_env" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="现象">
            <el-input v-model="form.symptom" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="前置条件">
            <el-input v-model="form.preconditions" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="排查步骤">
            <el-input v-model="form.check_steps" type="textarea" :rows="5" />
          </el-form-item>
          <el-form-item label="处理方法">
            <el-input v-model="form.handling_steps" type="textarea" :rows="4" />
          </el-form-item>
          <el-form-item label="恢复验证">
            <el-input v-model="form.recovery_verification" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="回滚方式">
            <el-input v-model="form.rollback_steps" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="注意事项">
            <el-input v-model="form.notes" type="textarea" :rows="2" />
          </el-form-item>
        </el-form>
        <article v-if="!canEdit" class="sop-reading-document">
          <header class="sop-document-intro"><span class="sop-document-mark"><AppIcon name="book" :size="23" /></span><div class="ops-eyebrow">OPERATIONS KNOWLEDGE</div><h2>{{ sop.title }}</h2><p>版本 v{{ sop.version }} <span>·</span> 维护人 {{ maintainerLabel(sop.maintainer_id) }}</p></header>
          <section v-for="(section, index) in [
            { title: '适用环境及版本', content: sop.applicable_env },
            { title: '现象', content: sop.symptom },
            { title: '前置条件', content: sop.preconditions },
            { title: '排查步骤', content: sop.check_steps },
            { title: '处理方法', content: sop.handling_steps },
            { title: '恢复验证', content: sop.recovery_verification },
            { title: '回滚方式', content: sop.rollback_steps },
            { title: '注意事项', content: sop.notes },
          ]" :key="section.title" class="sop-document-section">
            <div class="sop-section-heading"><span>{{ String(index + 1).padStart(2, '0') }}</span><h3>{{ section.title }}</h3></div>
            <div class="sop-document-text">{{ section.content || '未填写' }}</div>
          </section>
        </article>
      </div>

      <aside class="sop-metadata-column">
        <section v-if="canReview && sop.status === 'submitted'" class="ops-panel sop-review-panel">
          <div class="ops-section-header"><h2>审核意见</h2><AppIcon name="shield" :size="17" /></div>
          <p>核对内容后发布；退回时请填写修改建议。</p>
          <label class="form-label">审核意见(退回时必填)</label>
          <el-input v-model="reviewComment" type="textarea" :rows="3" placeholder="填写审核意见…" />
        </section>
        <div class="ops-panel sop-metadata-panel">
          <div class="ops-section-header"><h2>版本历史</h2><AppIcon name="history" :size="17" /></div>
          <div class="space-y-2">
            <div
              v-for="h in history"
              :key="h.id"
              class="sop-version-card"
              :class="{ 'is-active': h.id === sop.id }"
              @click="router.push({ name: 'sop-detail', params: { id: h.id } })"
            >
              <div class="flex items-center justify-between">
                <span class="font-medium">v{{ h.version }}</span>
                <el-tag size="small" :type="(sopStatusMap[h.status as SopStatus].type as any)">
                  {{ sopStatusMap[h.status as SopStatus].label }}
                </el-tag>
              </div>
              <div class="ops-text-faint mt-1 sop-version-time">{{ fmtTime(h.updated_at) }}</div>
            </div>
          </div>
        </div>

        <div class="ops-panel sop-metadata-panel">
          <div class="ops-section-header"><h2>审核信息</h2><AppIcon name="shield" :size="17" /></div>
          <div class="sop-meta-body">
            <div>审核意见: {{ sop.review_comment || '-' }}</div>
            <div>审核时间: {{ fmtTime(sop.reviewed_at) }}</div>
            <div>发布时间: {{ fmtTime(sop.published_at) }}</div>
            <div>维护人: {{ maintainerLabel(sop.maintainer_id) }}</div>
            <div v-if="sop.tags?.length">标签: {{ sop.tags.join('、') }}</div>
            <div v-if="sop.source_case">
              来源案例:
              <router-link class="sop-entity-link" :to="`/cases/${sop.source_case.id}`">
                #{{ sop.source_case.id }} {{ sop.source_case.title }}
              </router-link>
            </div>
            <div v-else-if="sop.source_case_id">来源案例 ID: {{ sop.source_case_id }}</div>
          </div>
        </div>

        <div class="ops-panel sop-metadata-panel" v-if="feedbacks.length">
          <div class="ops-section-header"><h2>使用反馈</h2><AppIcon name="check-circle" :size="17" /></div>
          <div class="space-y-2">
            <div v-for="f in feedbacks" :key="f.id" class="feedback-item">
              <el-tag size="small" effect="plain">{{ feedbackKindMap[f.kind] }}</el-tag>
              <span class="feedback-comment">{{ f.comment || '-' }}</span>
              <div class="ops-text-faint mt-1 sop-version-time">{{ fmtTime(f.created_at) }}</div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <el-dialog v-model="feedbackDialog" title="SOP 使用反馈" width="420px">
      <el-form label-position="top">
        <el-form-item label="反馈类型">
          <el-radio-group v-model="feedbackForm.kind">
            <el-radio value="effective">有效</el-radio>
            <el-radio value="ineffective">无效</el-radio>
            <el-radio value="suspected_outdated">疑似过期</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="feedbackForm.comment" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="feedbackDialog = false">取消</el-button>
        <el-button type="primary" :loading="feedbackSubmitting" @click="submitFeedback">提交反馈</el-button>
      </template>
    </el-dialog>

  </div>
</template>


<style scoped>
.sop-detail-page { max-width: 1480px; margin: 0 auto; }
.sop-page-header { align-items: flex-end; flex-wrap: wrap; }
.sop-page-heading { min-width: 0; flex: 1 1 400px; }
.sop-page-heading .ops-back-link { margin-bottom: 10px; }
.sop-page-heading h1 { margin: 7px 0 12px; font-size: 26px; line-height: 1.5; letter-spacing: -0.02em; overflow-wrap: anywhere; }
.sop-title-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.sop-title-mode { margin-left: 4px; color: var(--ops-muted); font-size: 12px; }
.sop-page-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; padding-bottom: 2px; }
.sop-page-actions :deep(.el-button) { margin-left: 0; }
.sop-workspace { display: grid; grid-template-columns: minmax(0, 1fr) 290px; gap: 22px; align-items: start; margin-top: 22px; }
.sop-document-panel { min-width: 0; padding: 30px 34px; }
.sop-editor-heading { display: flex; align-items: center; gap: 12px; margin-bottom: 23px; padding-bottom: 22px; border-bottom: 1px solid var(--ops-border); }
.section-icon { display: grid; place-items: center; width: 37px; height: 37px; flex-shrink: 0; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }
.sop-editor-heading h2 { margin: 0; color: var(--ops-text); font-size: 15px; font-weight: 650; }
.sop-editor-heading p { margin: 5px 0 0; color: var(--ops-muted); font-size: 12px; }
.sop-edit-form :deep(.el-form-item) { margin-bottom: 22px; }
.sop-edit-form :deep(.el-form-item__label) { margin-bottom: 8px; color: var(--ops-text); font-size: 12px; font-weight: 600; }
.sop-edit-form :deep(textarea) { font-size: 13px; line-height: 1.85; }
.sop-properties-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 20px; }
.sop-document-intro { padding-bottom: 26px; border-bottom: 1px solid var(--ops-border); }
.sop-document-mark { display: grid; place-items: center; width: 48px; height: 48px; margin-bottom: 22px; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }
.sop-document-intro .ops-eyebrow { font-size: 10px; letter-spacing: 1.5px; }
.sop-document-intro h2 { margin: 9px 0 13px; color: var(--ops-text); font-size: 25px; font-weight: 650; line-height: 1.6; letter-spacing: -0.02em; }
.sop-document-intro p { margin: 0; color: var(--ops-muted); font-size: 12px; }
.sop-document-intro p span { margin: 0 9px; color: var(--ops-border-strong); }
.sop-document-section { padding: 25px 0 0; }
.sop-section-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.sop-section-heading > span { color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 11px; }
.sop-section-heading h3 { margin: 0; color: var(--ops-text); font-size: 14px; font-weight: 650; }
.sop-document-text { color: var(--ops-text-soft); font-size: 14px; line-height: var(--ops-leading-reading); white-space: pre-wrap; overflow-wrap: anywhere; }
.sop-metadata-column { display: grid; gap: 18px; }
.sop-metadata-panel, .sop-review-panel { padding: 22px; }
.sop-metadata-panel h2, .sop-review-panel h2 { margin: 0; font-size: 14px; font-weight: 650; }
.sop-metadata-panel .ops-section-header, .sop-review-panel .ops-section-header { margin: -22px -22px 18px; }
.sop-metadata-panel .ops-section-header svg, .sop-review-panel .ops-section-header svg { color: var(--ops-faint); }
.sop-meta-body { display: flex; flex-direction: column; gap: 8px; color: var(--ops-text-soft); font-size: 12px; line-height: 1.9; }
.sop-version-time { font-family: var(--ops-font-mono); font-size: 10px; }
.sop-version-card { padding: 14px; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); font-size: 13px; cursor: pointer; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard), border-color var(--ops-duration-fast) var(--ops-ease-standard); }
.sop-version-card:hover { border-color: var(--ops-border-strong); background: var(--ops-surface-muted); }
.sop-version-card.is-active { border-color: var(--ops-brand); background: var(--ops-brand-soft); box-shadow: inset 2px 0 var(--ops-brand); }
.sop-review-panel { border-color: #eadcbb; background: #fffcf4; }
.sop-review-panel p { margin: 0 0 18px; color: #7a5410; font-size: 12px; line-height: 1.8; }
.feedback-item { padding: 10px; border: 1px solid var(--ops-border-light); border-radius: var(--ops-radius-control); background: var(--ops-surface-muted); font-size: 12px; }
.feedback-comment { margin-left: 8px; color: var(--ops-text-soft); }
@media (max-width: 1180px) {
  .sop-workspace { grid-template-columns: minmax(0, 1fr) 250px; gap: 18px; }
  .sop-document-panel { padding: 26px; }
  .sop-page-actions { justify-content: flex-start; }
}
@media (max-width: 900px) {
  .sop-workspace { grid-template-columns: minmax(0, 1fr); }
  .sop-metadata-column { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .sop-properties-grid, .sop-metadata-column { grid-template-columns: minmax(0, 1fr); }
  .sop-document-panel { padding: 22px; }
  .sop-page-heading h1 { font-size: 22px; }
  .sop-document-intro h2 { font-size: 21px; }
  .sop-title-mode { flex-basis: 100%; margin: 2px 0 0; }
}
.sop-entity-link { color: var(--ops-brand); text-decoration: none; overflow-wrap: anywhere; }
.sop-entity-link:hover { text-decoration: underline; }
</style>





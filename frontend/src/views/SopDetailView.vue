<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import { fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Category, FeedbackKind, Sop, SopFeedback, SopStatus, UserOption } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sop = ref<Sop | null>(null)
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
  const id = currentSopId()
  if (id == null) return false
  saving.value = true
  try {
    // 只回写服务端元数据,不回填表单:请求期间继续输入的内容不被响应覆盖
    const { data } = await client.patch(`/api/sops/${id}`, formPayload())
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
  const id = currentSopId()
  if (id == null) return
  submitting.value = true
  try {
    // 多步提交使用发起时的 ID 快照;等待期间切换历史版本不会改变请求目标
    await client.patch(`/api/sops/${id}`, formPayload())
    await client.post(`/api/sops/${id}/submit`, { comment: '' })
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
  if (id == null) return
  try {
    await client.post(`/api/sops/${id}/${path}`, { comment: comment ?? '' })
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
  if (id == null) return
  try {
    const { data } = await client.post(`/api/sops/${id}/new-version`)
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
  <div v-if="!sop" class="p-6">
    <div v-if="loading" class="text-gray-500">加载中…</div>
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
  <div class="p-6" v-else>
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-3">
        <el-button text @click="router.push('/sops')">← 返回</el-button>
        <h1 class="text-xl font-bold text-slate-800">{{ sop.title }}</h1>
        <el-tag size="small" effect="plain">v{{ sop.version }}</el-tag>
        <el-tag size="small" :type="(sopStatusMap[sop.status as SopStatus].type as any)">
          {{ sopStatusMap[sop.status as SopStatus].label }}
        </el-tag>
      </div>
      <div class="flex gap-2">
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

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 page-card">
        <!-- 保存/提交期间锁定编辑(审查 P2 #6):防止请求期间的新输入被提交后的旧快照覆盖 -->
        <el-form label-width="100px" :disabled="!canEdit || saving || submitting">
          <el-form-item label="标题">
            <el-input v-model="form.title" />
          </el-form-item>
          <div class="grid grid-cols-2 gap-2">
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
      </div>

      <div class="space-y-4">
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">版本历史</h2>
          <div class="space-y-2">
            <div
              v-for="h in history"
              :key="h.id"
              class="p-2 rounded border text-sm cursor-pointer hover:border-blue-300"
              :class="h.id === sop.id ? 'border-blue-400 bg-blue-50/40' : 'border-gray-100'"
              @click="router.push({ name: 'sop-detail', params: { id: h.id } })"
            >
              <div class="flex items-center justify-between">
                <span class="font-medium">v{{ h.version }}</span>
                <el-tag size="small" :type="(sopStatusMap[h.status as SopStatus].type as any)">
                  {{ sopStatusMap[h.status as SopStatus].label }}
                </el-tag>
              </div>
              <div class="text-xs text-gray-400 mt-1">{{ fmtTime(h.updated_at) }}</div>
            </div>
          </div>
        </div>

        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">审核信息</h2>
          <div class="text-sm space-y-2 text-gray-600">
            <div>审核意见: {{ sop.review_comment || '-' }}</div>
            <div>审核时间: {{ fmtTime(sop.reviewed_at) }}</div>
            <div>发布时间: {{ fmtTime(sop.published_at) }}</div>
            <div>维护人: {{ maintainerLabel(sop.maintainer_id) }}</div>
            <div v-if="sop.tags?.length">标签: {{ sop.tags.join('、') }}</div>
            <div v-if="sop.source_case_id">来源案例 ID: {{ sop.source_case_id }}</div>
          </div>
        </div>

        <div class="page-card" v-if="feedbacks.length">
          <h2 class="font-semibold text-slate-700 mb-3">使用反馈</h2>
          <div class="space-y-2">
            <div v-for="f in feedbacks" :key="f.id" class="p-2 rounded bg-slate-50 text-sm">
              <el-tag size="small" effect="plain">{{ feedbackKindMap[f.kind] }}</el-tag>
              <span class="ml-2 text-gray-600">{{ f.comment || '-' }}</span>
              <div class="text-xs text-gray-400 mt-1">{{ fmtTime(f.created_at) }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <el-dialog v-model="feedbackDialog" title="SOP 使用反馈" width="420px">
      <el-form label-width="80px">
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

    <!-- 审核意见条 -->
    <div
      v-if="canReview && sop.status === 'submitted'"
      class="fixed bottom-6 right-6 w-96 page-card shadow-lg"
    >
      <label class="form-label">审核意见(退回时必填)</label>
      <el-input v-model="reviewComment" type="textarea" :rows="2" placeholder="填写审核意见…" />
    </div>
  </div>
</template>

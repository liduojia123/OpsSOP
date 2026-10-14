<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import { caseStatusMap, fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Case, CaseStatus, Category } from '../types'

const auth = useAuthStore()
const cases = ref<Case[]>([])
const categories = ref<Category[]>([])
const loading = ref(false)
const filters = reactive({ q: '', status: '' as '' | CaseStatus })

const detailVisible = ref(false)
const current = ref<Case | null>(null)
const reviewComment = ref('')

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
    ElMessage.success('已生成 SOP 草稿')
    detailVisible.value = false
    window.location.href = `/sops/${data.id}`
  } catch (e) {
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
            <el-button
              v-if="current.status === 'approved' && current.created_by === auth.user?.id"
              type="primary"
              plain
              @click="generateSop"
            >
              生成 SOP 草稿
            </el-button>
          </template>
          <div v-if="current.status === 'pending_review'" class="text-xs text-gray-400 mt-2">
            待审核内容已锁定审核快照;如需修改请等待退回。
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

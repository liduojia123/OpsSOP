<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import { faultStatusMap, fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
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
  const { data } = await client.get(`/api/faults/${id}`)
  fault.value = data
  fillForm(data)
}

function formPayload() {
  return {
    ...form,
    occurred_at: form.occurred_at ? new Date(form.occurred_at).toISOString() : null,
    tags: [...form.tags],
  }
}

async function save() {
  if (!fault.value) return false
  saving.value = true
  try {
    await client.patch(`/api/faults/${fault.value.id}`, formPayload())
    ElMessage.success('已保存')
    await load()
    return true
  } catch (e) {
    ElMessage.error(errMsg(e))
    return false
  } finally {
    saving.value = false
  }
}

// 追加记录只更新 records 列表,不整页 reload,避免覆盖未保存的表单编辑
async function addRecord() {
  if (!fault.value || !recordForm.content.trim()) {
    ElMessage.warning('请输入记录内容')
    return
  }
  adding.value = true
  try {
    const { data } = await client.post(`/api/faults/${fault.value.id}/records`, {
      kind: recordForm.kind,
      content: recordForm.content,
    })
    recordForm.content = ''
    fault.value.records.push(data)
    ElMessage.success('已追加记录')
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留输入
  } finally {
    adding.value = false
  }
}

async function toggleStatus() {
  if (!fault.value || !canEdit.value) return
  const next = fault.value.status === 'draft' ? 'open' : 'draft'
  toggling.value = true
  try {
    await client.patch(`/api/faults/${fault.value.id}`, { status: next })
    ElMessage.success(next === 'open' ? '已转为处理中' : '已转为草稿')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    toggling.value = false
  }
}

// 确认解决前先保存表单并等待成功,确保案例取到最新故障内容;失败则中止并保留输入
async function doResolve() {
  if (!fault.value) return
  if (!resolveForm.actual_root_cause.trim() || !resolveForm.handling_steps.trim() || !resolveForm.recovery_verification.trim()) {
    ElMessage.warning('确认解决须填写实际根因、处理步骤和恢复验证')
    return
  }
  resolving.value = true
  try {
    await client.patch(`/api/faults/${fault.value.id}`, formPayload())
    await client.post(`/api/faults/${fault.value.id}/resolve`, { ...resolveForm })
    resolveDialog.value = false
    ElMessage.success('已确认解决,生成待审核案例')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留对话框与已填内容
  } finally {
    resolving.value = false
  }
}

onMounted(async () => {
  try {
    const [cats, st] = await Promise.all([
      client.get('/api/categories'),
      client.get('/api/system/ai-status'),
    ])
    categories.value = cats.data
    aiStatus.value = st.data
  } catch {
    /* 状态展示失败不影响业务 */
  }
  await load().catch((e) => ElMessage.error(errMsg(e)))
})
</script>

<template>
  <div class="h-full flex flex-col p-6 gap-4 overflow-hidden">
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-3">
        <el-button text @click="router.push('/faults')">← 返回</el-button>
        <h1 class="text-xl font-bold text-slate-800">
          {{ fault?.title ?? '加载中' }}
        </h1>
        <el-tag v-if="fault" size="small" :type="(faultStatusMap[fault.status].type as any)">
          {{ faultStatusMap[fault.status].label }}
        </el-tag>
      </div>
      <div class="flex gap-2">
        <el-button
          v-if="canEdit && fault?.status === 'draft'"
          :loading="toggling"
          @click="toggleStatus"
        >
          开始处理
        </el-button>
        <el-button
          v-if="canEdit && fault?.status === 'open'"
          :loading="toggling"
          @click="toggleStatus"
        >
          转为草稿
        </el-button>
        <el-button v-if="canEdit" type="primary" :loading="saving" @click="save">保存</el-button>
        <el-button
          v-if="canEdit && fault && fault.status !== 'resolved'"
          type="success"
          @click="resolveDialog = true"
        >
          确认解决
        </el-button>
      </div>
    </div>

    <div class="flex-1 flex gap-4 min-h-0">
      <!-- 左侧:录入区 -->
      <div class="flex-1 overflow-auto space-y-4 pr-1">
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">故障信息(原始证据)</h2>
          <el-form label-width="92px" :disabled="!canEdit">
            <el-form-item label="标题">
              <el-input v-model="form.title" />
            </el-form-item>
            <el-form-item label="详细描述">
              <el-input v-model="form.description" type="textarea" :rows="3" />
            </el-form-item>
            <div class="grid grid-cols-2 gap-2">
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
              <el-input v-model="form.error_logs" type="textarea" :rows="5" />
            </el-form-item>
            <el-form-item label="已尝试操作">
              <el-input v-model="form.attempted_actions" type="textarea" :rows="2" />
            </el-form-item>
          </el-form>
        </div>

        <!-- 追加记录 -->
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">检查结果与处理记录</h2>
          <div v-if="!fault || fault.records.length === 0" class="text-sm text-gray-400 mb-3">
            暂无记录。可在下方持续补充检查结果与处理动作。
          </div>
          <div v-else class="space-y-2 mb-4">
            <div
              v-for="r in fault.records"
              :key="r.id"
              class="p-3 rounded bg-slate-50 border border-slate-100"
            >
              <div class="flex items-center justify-between text-xs text-gray-400 mb-1">
                <el-tag size="small" :type="r.kind === 'check_result' ? 'primary' : 'warning'" effect="plain">
                  {{ r.kind === 'check_result' ? '检查结果' : '处理记录' }}
                </el-tag>
                <span>{{ fmtTime(r.created_at) }}</span>
              </div>
              <div class="text-sm text-slate-700 whitespace-pre-wrap">{{ r.content }}</div>
            </div>
          </div>
          <div v-if="canEdit && fault && fault.status !== 'resolved'" class="flex gap-2 items-start">
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
        </div>

        <!-- 人工确认结果 -->
        <div v-if="fault && fault.status === 'resolved'" class="page-card border-l-4 border-emerald-400">
          <h2 class="font-semibold text-emerald-700 mb-3">人工确认结果(已确认事实)</h2>
          <div class="space-y-3 text-sm">
            <div>
              <div class="text-gray-500 mb-1">实际根因</div>
              <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ fault.actual_root_cause }}</div>
            </div>
            <div>
              <div class="text-gray-500 mb-1">处理步骤</div>
              <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ fault.handling_steps }}</div>
            </div>
            <div>
              <div class="text-gray-500 mb-1">恢复验证</div>
              <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ fault.recovery_verification }}</div>
            </div>
            <div class="text-xs text-gray-400">确认时间: {{ fmtTime(fault.resolved_at) }}</div>
          </div>
        </div>
      </div>

      <!-- 右侧:AI 排查助手(第二阶段接入,当前为占位) -->
      <aside class="w-96 flex flex-col min-h-0">
        <div class="page-card flex-1 flex flex-col min-h-0 overflow-hidden">
          <div class="flex items-center justify-between mb-3">
            <h2 class="font-semibold text-slate-700">AI 排查助手</h2>
            <el-tag size="small" type="info">
              {{ aiStatus?.ai_mode === 'demo' ? '演示模式' : aiStatus?.ai_mode === 'disabled' ? '未启用' : '待接入' }}
            </el-tag>
          </div>

          <div class="flex-1 overflow-auto">
            <div class="text-sm text-gray-500 leading-6">
              <p class="mb-3">右侧助手将在第二阶段接入,提供:</p>
              <ul class="list-disc pl-5 space-y-1.5">
                <li>输入摘要与已知事实</li>
                <li>优先排查方向与候选原因(含依据、缺失信息、验证方法)</li>
                <li>待补信息与下一步检查建议</li>
                <li>相关案例与 SOP 推荐</li>
              </ul>
              <div class="mt-4 p-3 rounded bg-amber-50 border border-amber-200 text-amber-800 text-xs leading-5">
                当前 AI 模式:
                <b>{{ aiStatus?.message ?? '未获取到状态' }}</b><br />
                演示模式下的分析为模拟数据,将明确标注;真实模式失败不会返回预设答案。
              </div>
            </div>
          </div>
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
      <el-form label-width="92px">
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

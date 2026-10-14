<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import { faultStatusMap, fmtTime } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Category, Fault, FaultStatus } from '../types'

const router = useRouter()
const auth = useAuthStore()
const faults = ref<Fault[]>([])
const categories = ref<Category[]>([])
const loading = ref(false)

const filters = reactive({
  q: '',
  status: '' as '' | FaultStatus,
  category_id: '' as '' | number,
  service: '',
  environment: '',
  occurred_range: null as [Date, Date] | null,
})

const dialogVisible = ref(false)
const creating = ref(false)
const form = reactive({
  id: 0,
  title: '',
  description: '',
  occurred_at: null as Date | null,
  environment: 'prod',
  service: '',
  component_version: '',
  recent_changes: '',
  error_logs: '',
  attempted_actions: '',
  category_id: null as number | null,
  tags: [] as string[],
})

async function load() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    if (filters.q) params.q = filters.q
    if (filters.status) params.status = filters.status
    if (filters.category_id) params.category_id = filters.category_id
    if (filters.service) params.service = filters.service
    if (filters.environment) params.environment = filters.environment
    if (filters.occurred_range?.[0]) params.occurred_from = filters.occurred_range[0].toISOString()
    if (filters.occurred_range?.[1]) params.occurred_to = filters.occurred_range[1].toISOString()
    const { data } = await client.get('/api/faults', { params })
    faults.value = data
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.id = 0
  form.title = ''
  form.description = ''
  form.occurred_at = null
  form.environment = 'prod'
  form.service = ''
  form.component_version = ''
  form.recent_changes = ''
  form.error_logs = ''
  form.attempted_actions = ''
  form.category_id = null
  form.tags = []
  dialogVisible.value = true
}

async function save(asDraft: boolean) {
  if (!form.title.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  creating.value = true
  try {
    const payload = {
      title: form.title,
      description: form.description,
      occurred_at: form.occurred_at ? new Date(form.occurred_at).toISOString() : null,
      environment: form.environment,
      service: form.service,
      component_version: form.component_version,
      recent_changes: form.recent_changes,
      error_logs: form.error_logs,
      attempted_actions: form.attempted_actions,
      category_id: form.category_id,
      tags: [...form.tags],
      status: asDraft ? 'draft' : 'open',
    }
    const { data } = await client.post('/api/faults', payload)
    dialogVisible.value = false
    ElMessage.success('创建成功')
    router.push({ name: 'fault-detail', params: { id: data.id } })
  } catch (e) {
    ElMessage.error(errMsg(e)) // 失败保留已填内容
  } finally {
    creating.value = false
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
    <div class="flex items-center justify-between mb-4">
      <h1 class="text-xl font-bold text-slate-800">故障处理</h1>
      <el-button v-if="auth.role !== 'reviewer'" type="primary" @click="openCreate">
        + 新建故障
      </el-button>
    </div>

    <div class="page-card mb-4">
      <div class="flex flex-wrap gap-3 items-end">
        <div>
          <label class="form-label">关键词</label>
          <el-input v-model="filters.q" placeholder="标题/描述/日志" clearable class="!w-52" @keyup.enter="load" />
        </div>
        <div>
          <label class="form-label">状态</label>
          <el-select v-model="filters.status" clearable placeholder="全部" class="!w-32">
            <el-option label="草稿" value="draft" />
            <el-option label="处理中" value="open" />
            <el-option label="已解决" value="resolved" />
          </el-select>
        </div>
        <div>
          <label class="form-label">分类</label>
          <el-select v-model="filters.category_id" clearable placeholder="全部" class="!w-40">
            <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </div>
        <div>
          <label class="form-label">服务</label>
          <el-input v-model="filters.service" placeholder="服务名" clearable class="!w-36" />
        </div>
        <div>
          <label class="form-label">环境</label>
          <el-input v-model="filters.environment" placeholder="如 prod" clearable class="!w-28" />
        </div>
        <div>
          <label class="form-label">发生时间</label>
          <el-date-picker
            v-model="filters.occurred_range"
            type="datetimerange"
            start-placeholder="开始"
            end-placeholder="结束"
            class="!w-80"
          />
        </div>
        <el-button type="primary" plain @click="load">查询</el-button>
      </div>
    </div>

    <div class="page-card">
      <el-table :data="faults" v-loading="loading" empty-text="暂无故障记录">
        <el-table-column label="标题" min-width="220">
          <template #default="{ row }">
            <router-link :to="`/faults/${row.id}`" class="text-blue-600 hover:underline">
              {{ row.title }}
            </router-link>
          </template>
        </el-table-column>
        <el-table-column prop="service" label="服务" width="130" />
        <el-table-column prop="environment" label="环境" width="90" />
        <el-table-column label="发生时间" width="170">
          <template #default="{ row }">{{ row.occurred_at ? fmtTime(row.occurred_at) : '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="(faultStatusMap[row.status as FaultStatus].type as any)">
              {{ faultStatusMap[row.status as FaultStatus].label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" title="新建故障" width="680px">
      <el-form label-width="88px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" placeholder="简要描述故障现象" />
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
            <el-select v-model="form.environment" class="w-full">
              <el-option label="prod" value="prod" />
              <el-option label="staging" value="staging" />
              <el-option label="test" value="test" />
              <el-option label="dev" value="dev" />
            </el-select>
          </el-form-item>
          <el-form-item label="服务">
            <el-input v-model="form.service" placeholder="如 checkout" />
          </el-form-item>
          <el-form-item label="组件版本">
            <el-input v-model="form.component_version" placeholder="如 k8s 1.28" />
          </el-form-item>
          <el-form-item label="分类">
            <el-select v-model="form.category_id" clearable placeholder="选择分类" class="w-full">
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
          <el-input v-model="form.recent_changes" type="textarea" :rows="2" placeholder="发布/配置/流量变化" />
        </el-form-item>
        <el-form-item label="报错日志">
          <el-input v-model="form.error_logs" type="textarea" :rows="4" placeholder="粘贴关键日志文本(第一版不支持文件上传)" />
        </el-form-item>
        <el-form-item label="已尝试操作">
          <el-input v-model="form.attempted_actions" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :loading="creating" @click="save(true)">存为草稿</el-button>
        <el-button type="primary" :loading="creating" @click="save(false)">创建并处理</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import { fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Category, Sop, SopStatus } from '../types'

const router = useRouter()
const auth = useAuthStore()
const sops = ref<Sop[]>([])
const categories = ref<Category[]>([])
const loading = ref(false)
const filters = reactive({ q: '', status: '' as '' | SopStatus })
const createDialog = ref(false)
const newTitle = ref('')

async function load() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    if (filters.q) params.q = filters.q
    if (filters.status) params.status = filters.status
    const { data } = await client.get('/api/sops', { params })
    sops.value = data
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function createSop() {
  if (!newTitle.value.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  try {
    const { data } = await client.post('/api/sops', { title: newTitle.value })
    createDialog.value = false
    newTitle.value = ''
    ElMessage.success('已创建草稿')
    router.push({ name: 'sop-detail', params: { id: data.id } })
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function categoryName(id: number | null) {
  return categories.value.find((c) => c.id === id)?.name ?? '-'
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
      <h1 class="text-xl font-bold text-slate-800">SOP 知识库</h1>
      <el-button v-if="auth.role !== 'reviewer'" type="primary" @click="createDialog = true">
        + 新建 SOP
      </el-button>
    </div>

    <div class="page-card mb-4">
      <div class="flex flex-wrap gap-3 items-end">
        <div>
          <label class="form-label">关键词</label>
          <el-input v-model="filters.q" placeholder="标题/现象/步骤" clearable class="!w-56" @keyup.enter="load" />
        </div>
        <div>
          <label class="form-label">状态</label>
          <el-select v-model="filters.status" clearable placeholder="全部" class="!w-32">
            <el-option label="草稿" value="draft" />
            <el-option label="待审核" value="submitted" />
            <el-option label="已退回" value="returned" />
            <el-option label="已发布" value="published" />
            <el-option label="历史版本" value="superseded" />
            <el-option label="已废弃" value="deprecated" />
          </el-select>
        </div>
        <el-button type="primary" plain @click="load">查询</el-button>
      </div>
    </div>

    <div class="page-card">
      <el-table :data="sops" v-loading="loading" empty-text="暂无 SOP">
        <el-table-column label="标题" min-width="240">
          <template #default="{ row }">
            <router-link :to="`/sops/${row.id}`" class="text-blue-600 hover:underline">
              {{ row.title }}
            </router-link>
            <el-tag size="small" class="ml-2" effect="plain">v{{ row.version }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="120">
          <template #default="{ row }">{{ categoryName(row.category_id) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="(sopStatusMap[row.status as SopStatus].type as any)">
              {{ sopStatusMap[row.status as SopStatus].label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="发布时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.published_at) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="createDialog" title="新建 SOP 草稿" width="420px">
      <el-form label-width="60px">
        <el-form-item label="标题">
          <el-input v-model="newTitle" placeholder="SOP 标题" @keyup.enter="createSop" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" @click="createSop">创建草稿</el-button>
      </template>
    </el-dialog>
  </div>
</template>

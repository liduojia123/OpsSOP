<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Category, Sop, SopStatus } from '../types'

const router = useRouter()
const auth = useAuthStore()
const sops = ref<Sop[]>([])
const categories = ref<Category[]>([])
const loading = ref(false)
const loadError = ref('')
const filters = reactive({ q: '', status: '' as '' | SopStatus })
const createDialog = ref(false)
const newTitle = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const params: Record<string, unknown> = {}
    if (filters.q) params.q = filters.q
    if (filters.status) params.status = filters.status
    const { data } = await client.get('/api/sops', { params })
    sops.value = data
  } catch (e) {
    loadError.value = errMsg(e)
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
  <div class="ops-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">KNOWLEDGE / 标准操作</div>
        <h1>SOP 知识库</h1>
        <p class="ops-page-description">把已确认的处理经验，沉淀为团队可复用的标准操作。</p>
      </div>
      <div class="ops-page-actions">
        <el-button v-if="auth.role !== 'reviewer'" type="primary" @click="createDialog = true"><AppIcon name="plus" :size="16" />新建 SOP</el-button>
      </div>
    </header>

    <section class="ops-panel mb-5">
      <div class="ops-filter-bar">
        <div class="min-w-[220px] flex-1">
          <label class="form-label">检索知识</label>
          <el-input v-model="filters.q" placeholder="标题/现象/步骤" clearable @keyup.enter="load"><template #prefix><AppIcon name="search" :size="16" /></template></el-input>
        </div>
        <div class="w-44">
          <label class="form-label">发布状态</label>
          <el-select v-model="filters.status" clearable placeholder="全部" class="w-full">
            <el-option label="草稿" value="draft" /><el-option label="待审核" value="submitted" /><el-option label="已退回" value="returned" /><el-option label="已发布" value="published" /><el-option label="历史版本" value="superseded" /><el-option label="已废弃" value="deprecated" />
          </el-select>
        </div>
        <el-button type="primary" plain @click="load"><AppIcon name="search" :size="16" />查询</el-button>
      </div>
    </section>

    <section v-if="loadError" class="ops-panel ops-load-error">
      <el-alert type="error" :title="loadError" :closable="false" show-icon />
      <el-button type="primary" plain @click="load">重新加载</el-button>
    </section>

    <section v-else class="ops-panel ops-table-panel">
      <div class="ops-section-header">
        <div class="flex items-center gap-3"><h2>操作文档</h2><span class="ops-result-count">{{ sops.length }} 条结果</span></div>
        <span class="ops-section-caption flex items-center gap-1.5"><AppIcon name="shield" :size="14" />审核发布后进入正式知识</span>
      </div>
      <el-table :data="sops" v-loading="loading" empty-text="暂无 SOP">
        <el-table-column label="文档名称" min-width="340">
          <template #default="{ row }">
            <div class="flex items-center gap-3 py-1.5">
              <span class="ops-icon-tile"><AppIcon name="book" :size="20" /></span>
              <div class="min-w-0">
                <router-link :to="`/sops/${row.id}`" class="ops-entity-link">{{ row.title }}</router-link>
                <div class="ops-id mt-1 flex items-center gap-2"><span>SOP-{{ String(row.id).padStart(4, '0') }}</span><span>·</span><span>版本 v{{ row.version }}</span></div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="160"><template #default="{ row }"><span class="ops-text-muted">{{ categoryName(row.category_id) }}</span></template></el-table-column>
        <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag size="small" :type="(sopStatusMap[row.status as SopStatus].type as any)">{{ sopStatusMap[row.status as SopStatus].label }}</el-tag></template></el-table-column>
        <el-table-column label="发布时间" width="190"><template #default="{ row }">{{ fmtTime(row.published_at) }}</template></el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="createDialog" title="新建 SOP 草稿" width="min(480px, 94vw)" class="ops-dialog">
      <div class="ops-icon-tile mb-5"><AppIcon name="book" :size="24" /></div>
      <p class="ops-page-description mb-5">先给文档一个清晰的标题，再补充环境、步骤与恢复验证。草稿保存后仍需审核发布。</p>
      <el-form label-position="top"><el-form-item label="标题"><el-input v-model="newTitle" placeholder="SOP 标题" @keyup.enter="createSop" /></el-form-item></el-form>
      <template #footer><el-button @click="createDialog = false">取消</el-button><el-button type="primary" @click="createSop">创建草稿<AppIcon name="arrow-right" :size="16" /></el-button></template>
    </el-dialog>
  </div>
</template>




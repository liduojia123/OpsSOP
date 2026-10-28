<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { fmtTime, roleMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { AuditEvent, Category, Role, User } from '../types'

const auth = useAuthStore()
const router = useRouter()
const users = ref<User[]>([])
const categories = ref<Category[]>([])
const tab = ref('users')
const savingUser = ref(false)
const auditEvents = ref<AuditEvent[]>([])
const auditAction = ref('')
const auditActor = ref<number | undefined>()
const auditDates = ref<[Date, Date] | null>(null)
const userSearch = ref('')
const filteredUsers = computed(() => {
  const term = userSearch.value.trim().toLowerCase()
  return term ? users.value.filter((u) => `${u.username} ${u.display_name}`.toLowerCase().includes(term)) : users.value
})
const auditLoading = ref(false)
const auditError = ref('')
const auditCursor = ref<number | null>(null)
const auditHistory = ref<(number | null)[]>([])
const auditHasMore = ref(false)
let appliedAuditFilters: Record<string, string | number> = {}

const userDialog = ref(false)
const userForm = reactive({
  id: 0,
  username: '',
  display_name: '',
  password: '',
  role: 'operator' as Role,
})
const catDialog = ref(false)
const catForm = reactive({ id: 0, name: '', description: '', sort_order: 0 })

async function loadAll() {
  const [u, c] = await Promise.all([
    client.get('/api/users'),
    client.get('/api/categories'),
  ])
  users.value = u.data
  categories.value = c.data
}

function openUserCreate() {
  userForm.id = 0
  userForm.username = ''
  userForm.display_name = ''
  userForm.password = ''
  userForm.role = 'operator'
  userDialog.value = true
}

function openUserEdit(user: User) {
  Object.assign(userForm, { id: user.id, username: user.username, display_name: user.display_name, password: '', role: user.role })
  userDialog.value = true
}

async function loadAudit(direction: 'search' | 'next' | 'previous' = 'search') {
  if (auditLoading.value) return
  const filters: Record<string, string | number> = direction === 'search' ? {} : { ...appliedAuditFilters }
  if (direction === 'search') {
    if (auditAction.value.trim()) filters.action = auditAction.value.trim()
    if (auditActor.value != null) filters.actor_id = auditActor.value
    if (auditDates.value) {
      filters.start = auditDates.value[0].toISOString()
      filters.end = auditDates.value[1].toISOString()
    }
  }
  const cursor = direction === 'next' ? auditEvents.value.at(-1)?.id ?? null : direction === 'previous' ? auditHistory.value.at(-1) ?? null : null
  auditLoading.value = true
  auditError.value = ''
  try {
    const { data } = await client.get('/api/audit', { params: { ...filters, limit: 51, ...(cursor === null ? {} : { before_id: cursor }) } })
    if (direction === 'search') { auditHistory.value = []; appliedAuditFilters = filters }
    if (direction === 'next') auditHistory.value.push(auditCursor.value)
    if (direction === 'previous') auditHistory.value.pop()
    auditCursor.value = cursor
    auditHasMore.value = data.length > 50
    auditEvents.value = data.slice(0, 50)
  } catch (e) {
    auditEvents.value = []
    auditError.value = errMsg(e)
  } finally {
    auditLoading.value = false
  }
}

function actorName(id: number | null) {
  if (id === null) return '未登录或系统'
  const user = users.value.find((item) => item.id === id)
  return user ? `${user.display_name} (${user.username})` : `用户 #${id}`
}

async function saveUser() {
  if (savingUser.value) return
  if (!userForm.username.trim() || (!userForm.id && !userForm.password.trim())) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  if (userForm.id && !userForm.display_name.trim()) {
    ElMessage.warning('请填写显示名')
    return
  }
  savingUser.value = true
  try {
    if (userForm.id) {
      const { data } = await client.patch(`/api/users/${userForm.id}`, {
        display_name: userForm.display_name.trim(),
        role: userForm.role,
      })
      if (auth.user?.id === data.id) auth.setSession(auth.token, data)
    } else {
      await client.post('/api/users', {
        username: userForm.username,
        password: userForm.password,
        display_name: userForm.display_name || userForm.username,
        role: userForm.role,
      })
    }
    userDialog.value = false
    ElMessage.success('已保存')
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    savingUser.value = false
  }
}

async function toggleActive(u: User) {
  try {
    await client.patch(`/api/users/${u.id}`, { is_active: !u.is_active })
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function resetPassword(u: User) {
  const { value } = await ElMessageBox.prompt(`为 ${u.username} 设置新密码`, '重置密码', {
    inputType: 'password',
  }).catch(() => ({ value: null }))
  if (!value) return
  try {
    await client.patch(`/api/users/${u.id}`, { password: value })
    if (u.id === auth.user?.id) {
      auth.logout()
      ElMessage.success('密码已重置，请重新登录')
      await router.replace({ name: 'login' })
    } else {
      ElMessage.success('密码已重置，原会话已失效')
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function openCatCreate() {
  catForm.id = 0
  catForm.name = ''
  catForm.description = ''
  catForm.sort_order = 0
  catDialog.value = true
}

function openCatEdit(c: Category) {
  catForm.id = c.id
  catForm.name = c.name
  catForm.description = c.description
  catForm.sort_order = c.sort_order
  catDialog.value = true
}

async function saveCat() {
  if (!catForm.name.trim()) {
    ElMessage.warning('请填写分类名')
    return
  }
  try {
    if (catForm.id) {
      await client.patch(`/api/categories/${catForm.id}`, {
        name: catForm.name,
        description: catForm.description,
        sort_order: catForm.sort_order,
      })
    } else {
      await client.post('/api/categories', {
        name: catForm.name,
        description: catForm.description,
        sort_order: catForm.sort_order,
      })
    }
    catDialog.value = false
    ElMessage.success('已保存')
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function removeCat(c: Category) {
  await ElMessageBox.confirm(`确认删除分类「${c.name}」?`, '提示', { type: 'warning' }).catch(() => {
    throw new Error('cancel')
  })
  try {
    await client.delete(`/api/categories/${c.id}`)
    ElMessage.success('已删除')
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(async () => {
  try {
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
})
</script>

<template>
  <div class="ops-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">WORKSPACE / 团队设置</div>
        <h1>系统管理</h1>
        <p class="ops-page-description">管理团队访问、账号状态和知识分类，让协作保持有序。</p>
      </div>
      <span class="ops-chip"><AppIcon name="shield" :size="15" />管理员权限</span>
    </header>

    <div class="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
      <div class="ops-panel admin-stat">
        <div><p class="admin-stat-label">团队账号</p><p class="admin-stat-value">{{ users.length }}<span>位成员</span></p></div>
        <span class="ops-icon-tile"><AppIcon name="user" :size="21" /></span>
      </div>
      <div class="ops-panel admin-stat">
        <div><p class="admin-stat-label">启用账号</p><p class="admin-stat-value">{{ users.filter((u) => u.is_active).length }}<span>可访问工作区</span></p></div>
        <span class="ops-icon-tile"><AppIcon name="check" :size="21" /></span>
      </div>
      <div class="ops-panel admin-stat">
        <div><p class="admin-stat-label">知识分类</p><p class="admin-stat-value">{{ categories.length }}<span>个分类</span></p></div>
        <span class="ops-icon-tile is-warm"><AppIcon name="folder" :size="21" /></span>
      </div>
    </div>

    <el-tabs v-model="tab" @tab-change="(name: string | number) => { if (name === 'audit') loadAudit() }">
      <el-tab-pane label="账号管理" name="users">
        <section class="ops-panel ops-table-panel">
          <div class="ops-section-header">
            <div><h2>团队成员</h2><p class="ops-section-caption mt-1">根据职责分配角色，独立管理每位成员的访问状态。</p></div>
            <div class="admin-user-tools"><el-input v-model="userSearch" placeholder="搜索账号或显示名" clearable aria-label="搜索账号或显示名" /><el-button type="primary" @click="openUserCreate"><AppIcon name="plus" :size="16" />新建账号</el-button></div>
          </div>
          <el-table :data="filteredUsers" empty-text="没有匹配账号">
            <el-table-column label="账号" min-width="230">
              <template #default="{ row }">
                <div class="admin-member">
                  <span class="ops-initial">{{ (row.display_name || row.username).slice(0, 1).toUpperCase() }}</span>
                  <div><div class="admin-member-name">{{ row.display_name || row.username }}</div><div class="ops-id mt-0.5">{{ row.username }}</div></div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="角色" width="150"><template #default="{ row }"><el-tag size="small" :type="row.role === 'admin' ? 'warning' : row.role === 'reviewer' ? 'info' : 'success'" effect="plain">{{ roleMap[row.role as Role] }}</el-tag></template></el-table-column>
            <el-table-column label="状态" width="130"><template #default="{ row }"><el-tag size="small" :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="270" align="right">
              <template #default="{ row }">
                <el-button size="small" @click="openUserEdit(row)">编辑</el-button>
                <el-button size="small" @click="resetPassword(row)">重置密码</el-button>
                <el-button size="small" :type="row.is_active ? 'danger' : 'success'" plain @click="toggleActive(row)">{{ row.is_active ? '停用' : '启用' }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </section>
      </el-tab-pane>
      <el-tab-pane label="审计记录" name="audit">
        <section class="ops-panel ops-table-panel">
          <div class="ops-section-header">
            <div><h2>操作记录</h2><p class="ops-section-caption mt-1">按时间、操作人和事件筛选，每页 50 条。</p></div>
          </div>
          <div class="admin-audit-filter">
            <el-input v-model="auditAction" :disabled="auditLoading" placeholder="事件名称，如 user.update" clearable @keyup.enter="loadAudit()" />
            <el-select v-model="auditActor" :disabled="auditLoading" placeholder="全部操作人" clearable filterable aria-label="筛选操作人"><el-option v-for="user in users" :key="user.id" :label="`${user.display_name} (${user.username})`" :value="user.id" /></el-select>
            <el-date-picker v-model="auditDates" :disabled="auditLoading" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" />
            <el-button :loading="auditLoading" @click="loadAudit()">查询</el-button>
          </div>
          <el-alert v-if="auditError" title="审计记录加载失败" :description="auditError" type="error" :closable="false" show-icon />
          <el-table v-else v-loading="auditLoading" :data="auditEvents" empty-text="暂无审计记录">
            <el-table-column label="时间" width="180"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作人" min-width="150"><template #default="{ row }">{{ actorName(row.actor_id) }}</template></el-table-column>
            <el-table-column prop="action" label="事件" min-width="160" />
            <el-table-column label="对象" min-width="120"><template #default="{ row }">{{ row.target_type }} #{{ row.target_id }}</template></el-table-column>
            <el-table-column label="详情" min-width="220"><template #default="{ row }"><span class="admin-audit-detail">{{ JSON.stringify(row.detail) }}</span></template></el-table-column>
          </el-table>
          <div class="admin-audit-pages"><el-button :disabled="auditLoading || auditHistory.length === 0" @click="loadAudit('previous')">上一页</el-button><el-button :disabled="auditLoading || !auditHasMore" @click="loadAudit('next')">下一页</el-button></div>
        </section>
      </el-tab-pane>
      <el-tab-pane label="分类管理" name="categories">
        <section class="ops-panel ops-table-panel">
          <div class="ops-section-header">
            <div><h2>知识分类</h2><p class="ops-section-caption mt-1">统一故障与知识的组织方式，保留清晰的检索入口。</p></div>
            <el-button type="primary" @click="openCatCreate"><AppIcon name="plus" :size="16" />新建分类</el-button>
          </div>
          <el-table :data="categories" empty-text="暂无分类">
            <el-table-column label="分类名" min-width="180"><template #default="{ row }"><span class="admin-category-name"><AppIcon name="folder" :size="17" /><span>{{ row.name }}</span></span></template></el-table-column>
            <el-table-column prop="description" label="描述" min-width="220" />
            <el-table-column label="类型" width="130"><template #default="{ row }"><el-tag size="small" :type="row.is_system ? 'primary' : 'info'" effect="plain">{{ row.is_system ? '内置' : '自定义' }}</el-tag></template></el-table-column>
            <el-table-column prop="sort_order" label="排序" width="90" />
            <el-table-column label="操作" width="160" align="right"><template #default="{ row }"><el-button size="small" @click="openCatEdit(row)"><AppIcon name="edit" :size="13" />编辑</el-button><el-button v-if="!row.is_system" size="small" type="danger" plain @click="removeCat(row)">删除</el-button></template></el-table-column>
          </el-table>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="userDialog" :title="userForm.id ? '编辑账号' : '新建账号'" width="min(480px, 94vw)" class="ops-dialog">
      <p class="ops-page-description mb-5">为成员设置独立账号，并按实际工作职责选择角色。</p>
      <el-form label-position="top">
        <el-form-item label="用户名"><el-input v-model="userForm.username" :disabled="!!userForm.id" placeholder="用于登录的账号名称" /></el-form-item>
        <el-form-item label="显示名"><el-input v-model="userForm.display_name" placeholder="成员在工作区中的名称" /></el-form-item>
        <el-form-item v-if="!userForm.id" label="密码"><el-input v-model="userForm.password" type="password" show-password placeholder="设置初始密码" /></el-form-item>
        <el-form-item label="角色"><el-select v-model="userForm.role" class="w-full" :disabled="userForm.id === auth.user?.id"><el-option label="运维人员" value="operator" /><el-option label="审核人员" value="reviewer" /><el-option label="管理员" value="admin" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button :disabled="savingUser" @click="userDialog = false">取消</el-button><el-button type="primary" :loading="savingUser" @click="saveUser">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="catDialog" :title="catForm.id ? '编辑分类' : '新建分类'" width="min(480px, 94vw)" class="ops-dialog">
      <p class="ops-page-description mb-5">使用团队熟悉的分类名称，让问题归档和知识检索更容易。</p>
      <el-form label-position="top">
        <el-form-item label="分类名"><el-input v-model="catForm.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="catForm.description" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="排序"><el-input-number v-model="catForm.sort_order" :min="0" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="catDialog = false">取消</el-button><el-button type="primary" @click="saveCat">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admin-stat { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 20px; }
.admin-stat-label { margin: 0; color: var(--ops-muted); font-size: 11px; }
.admin-stat-value { margin: 8px 0 0; color: var(--ops-text); font-family: var(--ops-font-mono); font-size: 26px; font-weight: 500; letter-spacing: -1px; line-height: 1.2; }
.admin-stat-value > span { margin-left: 8px; color: var(--ops-muted); font-family: var(--ops-font-body); font-size: 11px; font-weight: 400; letter-spacing: 0; }
.admin-member { display: flex; align-items: center; gap: 12px; padding: 4px 0; min-width: 0; }
.admin-member-name { color: var(--ops-text); font-weight: 600; }
.admin-category-name { display: flex; align-items: center; gap: 10px; padding: 4px 0; color: var(--ops-text); font-weight: 550; }
.admin-category-name > svg { color: var(--ops-brand); }
.admin-audit-filter, .admin-user-tools { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.admin-audit-filter { margin-bottom: 16px; }
.admin-audit-filter > .el-input, .admin-audit-filter > .el-select, .admin-user-tools > .el-input { width: 200px; }
.admin-audit-pages { display: flex; justify-content: flex-end; margin-top: 16px; }
.admin-audit-detail { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>

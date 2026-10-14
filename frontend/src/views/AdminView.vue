<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import { fmtTime, roleMap } from '../labels'
import type { Category, Role, User } from '../types'

const users = ref<User[]>([])
const categories = ref<Category[]>([])
const tab = ref('users')

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

async function saveUser() {
  if (!userForm.username.trim() || !userForm.password.trim()) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  try {
    if (userForm.id) {
      await client.patch(`/api/users/${userForm.id}`, {
        display_name: userForm.display_name,
        role: userForm.role,
      })
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
    ElMessage.success('密码已重置')
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
  <div class="p-6">
    <h1 class="text-xl font-bold text-slate-800 mb-4">系统管理</h1>

    <el-tabs v-model="tab">
      <el-tab-pane label="账号管理" name="users">
        <div class="page-card">
          <div class="flex justify-end mb-3">
            <el-button type="primary" @click="openUserCreate">+ 新建账号</el-button>
          </div>
          <el-table :data="users" empty-text="暂无账号">
            <el-table-column prop="username" label="用户名" width="140" />
            <el-table-column prop="display_name" label="显示名" width="140" />
            <el-table-column label="角色" width="120">
              <template #default="{ row }">
                <el-tag size="small">{{ roleMap[row.role as Role] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.is_active ? 'success' : 'danger'">
                  {{ row.is_active ? '启用' : '停用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="220">
              <template #default="{ row }">
                <el-button size="small" @click="resetPassword(row)">重置密码</el-button>
                <el-button
                  size="small"
                  :type="row.is_active ? 'danger' : 'success'"
                  plain
                  @click="toggleActive(row)"
                >
                  {{ row.is_active ? '停用' : '启用' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="分类管理" name="categories">
        <div class="page-card">
          <div class="flex justify-end mb-3">
            <el-button type="primary" @click="openCatCreate">+ 新建分类</el-button>
          </div>
          <el-table :data="categories" empty-text="暂无分类">
            <el-table-column prop="name" label="分类名" width="180" />
            <el-table-column prop="description" label="描述" min-width="200" />
            <el-table-column label="类型" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.is_system ? 'primary' : 'info'" effect="plain">
                  {{ row.is_system ? '内置' : '自定义' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="sort_order" label="排序" width="80" />
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button size="small" @click="openCatEdit(row)">编辑</el-button>
                <el-button
                  v-if="!row.is_system"
                  size="small"
                  type="danger"
                  plain
                  @click="removeCat(row)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="userDialog" :title="userForm.id ? '编辑账号' : '新建账号'" width="420px">
      <el-form label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="userForm.username" :disabled="!!userForm.id" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="userForm.display_name" />
        </el-form-item>
        <el-form-item v-if="!userForm.id" label="密码">
          <el-input v-model="userForm.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="userForm.role" class="w-full">
            <el-option label="运维人员" value="operator" />
            <el-option label="审核人员" value="reviewer" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userDialog = false">取消</el-button>
        <el-button type="primary" @click="saveUser">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="catDialog" :title="catForm.id ? '编辑分类' : '新建分类'" width="420px">
      <el-form label-width="80px">
        <el-form-item label="分类名">
          <el-input v-model="catForm.name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="catForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="catForm.sort_order" :min="0" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="catDialog = false">取消</el-button>
        <el-button type="primary" @click="saveCat">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

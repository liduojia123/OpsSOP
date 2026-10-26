<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import { safeRedirect } from '../safeRedirect'
import { useAuthStore } from '../stores/auth'
import type { User } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function doLogin() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const { data } = await client.post('/api/auth/login', form)
    auth.setSession(data.token, data.user as User)
    ElMessage.success(`欢迎,${data.user.display_name}`)
    // 登录后回到被拦截的来源(仅站内路径);无 redirect 时进入工作台
    const target = safeRedirect(route.query.redirect)
    if (target) {
      await router.replace(target)
    } else {
      await router.replace({ name: 'workbench' })
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="h-full flex items-center justify-center bg-gradient-to-br from-slate-800 to-slate-600">
    <div class="w-96 bg-white rounded-xl shadow-xl p-8">
      <h1 class="text-2xl font-bold text-slate-800 mb-1">OpsSOP</h1>
      <p class="text-sm text-gray-500 mb-6">运维故障知识库与 SOP 管理系统</p>
      <el-form @submit.prevent="doLogin">
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="用户名" @keyup.enter="doLogin" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            @keyup.enter="doLogin"
          />
        </el-form-item>
        <el-button type="primary" class="w-full" :loading="loading" @click="doLogin">
          登 录
        </el-button>
      </el-form>
    </div>
  </div>
</template>

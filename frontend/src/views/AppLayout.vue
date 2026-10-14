<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import client from '../api'
import { useAuthStore } from '../stores/auth'
import { roleMap } from '../labels'
import type { AiStatus } from '../types'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const aiStatus = ref<AiStatus | null>(null)

const menus = computed(() => {
  const items = [
    { name: 'workbench', title: '工作台', path: '/' },
    { name: 'faults', title: '故障处理', path: '/faults' },
    { name: 'cases', title: '案例库', path: '/cases' },
    { name: 'sops', title: 'SOP 知识库', path: '/sops' },
  ]
  if (auth.role === 'admin') {
    items.push({ name: 'admin', title: '系统管理', path: '/admin' })
  }
  return items
})

const aiModeLabel = computed(() => {
  const map: Record<string, string> = {
    disabled: 'AI 未启用',
    demo: 'AI 演示模式(模拟数据)',
    cloud: 'AI 云端模式',
    local: 'AI 本地模式',
  }
  return aiStatus.value ? map[aiStatus.value.ai_mode] ?? aiStatus.value.ai_mode : '...'
})

onMounted(async () => {
  try {
    const { data } = await client.get('/api/system/ai-status')
    aiStatus.value = data
  } catch {
    aiStatus.value = null
  }
})

function logout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div class="h-full flex">
    <aside class="w-56 bg-slate-800 text-white flex flex-col">
      <div class="px-5 py-5 border-b border-slate-700">
        <div class="text-lg font-bold">OpsSOP</div>
        <div class="text-xs text-slate-400 mt-1">故障知识库 / SOP</div>
      </div>
      <nav class="flex-1 py-3">
        <router-link
          v-for="m in menus"
          :key="m.name"
          :to="m.path"
          class="block px-5 py-2.5 text-sm hover:bg-slate-700 transition-colors"
          :class="{
            'bg-slate-700 border-l-4 border-blue-400':
              route.name === m.name || (m.name === 'faults' && route.name === 'fault-detail') || (m.name === 'sops' && route.name === 'sop-detail'),
          }"
        >
          {{ m.title }}
        </router-link>
      </nav>
      <div class="px-5 py-4 border-t border-slate-700 text-xs">
        <div class="mb-2">
          <span
            class="inline-block w-2 h-2 rounded-full mr-1.5"
            :class="aiStatus?.ai_mode === 'disabled' ? 'bg-slate-500' : 'bg-emerald-400'"
          ></span>
          {{ aiModeLabel }}
        </div>
        <div class="flex items-center justify-between">
          <div>
            <div class="text-white">{{ auth.user?.display_name }}</div>
            <div class="text-slate-400">
              {{ auth.user ? roleMap[auth.user.role] : '' }}
            </div>
          </div>
          <el-button size="small" text class="!text-slate-300" @click="logout">退出</el-button>
        </div>
      </div>
    </aside>
    <main class="flex-1 overflow-auto">
      <router-view />
    </main>
  </div>
</template>

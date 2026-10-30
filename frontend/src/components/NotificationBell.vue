<script setup lang="ts">
/**
 * 站内通知铃铛:未读计数 + 下拉列表。
 * 只读取当前用户自己的通知;点击条目标记已读并跳转到对应实体。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import AppIcon from './AppIcon.vue'
import { fmtTime } from '../labels'
import type { NotificationItem } from '../types'

const route = useRoute()
const router = useRouter()
const unread = ref(0)
const items = ref<NotificationItem[]>([])
const open = ref(false)
const loading = ref(false)
let timer: number | undefined

const kindIcons: Record<string, string> = {
  'case.submitted': 'files',
  'case.approved': 'check-circle',
  'case.returned': 'history',
  'case.sop_published': 'book',
  'sop.submitted': 'book',
  'sop.published': 'check-circle',
  'sop.returned': 'history',
  'sop.deprecated': 'close',
  'fault.alert': 'activity',
}

async function loadCount() {
  try {
    const { data } = await client.get<{ count: number }>('/api/notifications/unread-count')
    unread.value = data.count
  } catch {
    // 未读计数失败不打断页面,顶部铃铛保持上一次状态
  }
}

async function loadList() {
  loading.value = true
  try {
    const { data } = await client.get<NotificationItem[]>('/api/notifications', {
      params: { limit: 15 },
    })
    items.value = data
    unread.value = data.filter((row) => !row.read_at).length
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function onOpenChange(value: boolean) {
  open.value = value
  if (value) await loadList()
}

async function openItem(item: NotificationItem) {
  if (!item.read_at) {
    try {
      await client.post(`/api/notifications/${item.id}/read`)
      item.read_at = new Date().toISOString()
      unread.value = Math.max(0, unread.value - 1)
    } catch {
      // 标记已读失败不阻止跳转;下次打开会重新同步
    }
  }
  open.value = false
  if (item.link) await router.push(item.link)
}

async function readAll() {
  try {
    const { data } = await client.post<{ updated: number }>('/api/notifications/read-all')
    const now = new Date().toISOString()
    for (const row of items.value) row.read_at = row.read_at ?? now
    unread.value = 0
    ElMessage.success(`已标记 ${data.updated} 条为已读`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(() => {
  void loadCount()
  timer = window.setInterval(loadCount, 60000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})

watch(() => route.fullPath, () => {
  open.value = false
  void loadCount()
})
</script>

<template>
  <el-popover
    placement="bottom-end"
    :width="360"
    trigger="click"
    :visible="open"
    @update:visible="onOpenChange"
  >
    <template #reference>
      <button class="ops-bell" :aria-label="`通知，${unread} 条未读`" :aria-expanded="open">
        <AppIcon name="bell" :size="18" />
        <span v-if="unread > 0" class="ops-bell-badge">{{ unread > 99 ? '99+' : unread }}</span>
      </button>
    </template>
    <div class="ops-bell-panel">
      <div class="ops-bell-head">
        <strong>通知</strong>
        <button v-if="unread > 0" class="ops-bell-readall" @click="readAll">全部已读</button>
      </div>
      <div v-if="loading" class="ops-bell-empty">正在读取…</div>
      <div v-else-if="!items.length" class="ops-bell-empty">暂无通知</div>
      <ul v-else class="ops-bell-list">
        <li
          v-for="item in items"
          :key="item.id"
          class="ops-bell-item"
          :class="{ 'is-unread': !item.read_at }"
          @click="openItem(item)"
        >
          <span class="ops-bell-item-icon"><AppIcon :name="kindIcons[item.kind] || 'bell'" :size="15" /></span>
          <div class="ops-bell-item-body">
            <div class="ops-bell-item-title">{{ item.title }}</div>
            <div v-if="item.body" class="ops-bell-item-text">{{ item.body }}</div>
            <div class="ops-bell-item-time">{{ fmtTime(item.created_at) }}</div>
          </div>
          <span v-if="!item.read_at" class="ops-bell-unread-dot" aria-label="未读"></span>
        </li>
      </ul>
    </div>
  </el-popover>
</template>

<style scoped>
.ops-bell { position: relative; display: grid; place-items: center; width: 32px; height: 32px; border: 0; border-radius: 4px; background: transparent; color: var(--ops-muted); cursor: pointer; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard), color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-bell:hover { background: var(--ops-surface-warm); color: var(--ops-text); }
.ops-bell-badge { position: absolute; top: 1px; right: 0; min-width: 15px; height: 15px; padding: 0 3px; border: 1px solid #f8f6f0; border-radius: 8px; background: var(--ops-brand); color: #fff; font-size: 9px; font-weight: 700; line-height: 13px; text-align: center; }
.ops-bell-panel { margin: -12px; }
.ops-bell-head { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; border-bottom: 1px solid var(--ops-border-light); }
.ops-bell-head strong { font-size: 13px; }
.ops-bell-readall { border: 0; background: transparent; color: var(--ops-brand); cursor: pointer; font-size: 11px; }
.ops-bell-empty { padding: 22px 14px; color: var(--ops-muted); font-size: 12px; text-align: center; }
.ops-bell-list { max-height: 380px; margin: 0; padding: 0; overflow-y: auto; list-style: none; }
.ops-bell-item { display: flex; gap: 10px; padding: 11px 14px; border-bottom: 1px solid var(--ops-border-light); cursor: pointer; }
.ops-bell-item:last-child { border-bottom: 0; }
.ops-bell-item:hover { background: var(--ops-surface-warm); }
.ops-bell-item.is-unread { background: color-mix(in srgb, var(--ops-brand-soft) 45%, transparent); }
.ops-bell-item-icon { display: grid; place-items: center; width: 28px; height: 28px; flex-shrink: 0; border: 1px solid var(--ops-border); border-radius: 4px; color: var(--ops-muted); }
.ops-bell-item-body { min-width: 0; flex: 1; }
.ops-bell-item-title { color: var(--ops-text); font-size: 12px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
.ops-bell-item-text { margin-top: 2px; color: var(--ops-muted); font-size: 11px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ops-bell-item-time { margin-top: 4px; color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 9px; }
.ops-bell-unread-dot { width: 6px; height: 6px; flex-shrink: 0; margin-top: 6px; border-radius: 50%; background: var(--ops-brand); }
</style>
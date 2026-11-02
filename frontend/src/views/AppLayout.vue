<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import AppLogo from '../components/AppLogo.vue'
import NotificationBell from '../components/NotificationBell.vue'
import ProfileDialog from '../components/ProfileDialog.vue'
import SignalBackdrop from '../components/SignalBackdrop.vue'
import FeedbackPreview from '../components/FeedbackPreview.vue'
import client, { errMsg } from '../api'
import { useAuthStore } from '../stores/auth'
import { roleMap } from '../labels'
import type { AiStatus, User } from '../types'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const aiStatus = ref<AiStatus | null>(null)
const statusLoading = ref(true)
const mobileMenuOpen = ref(false)
const sidebarPreferenceKey = 'opssop_sidebar_collapsed'
const sidebarCollapsed = ref(false)
try {
  sidebarCollapsed.value = localStorage.getItem(sidebarPreferenceKey) === '1'
} catch {
  // 存储不可用时仍允许本次页面内切换。
}
const mainEl = ref<HTMLElement | null>(null)
const profileDialog = ref(false)
const feedbackPreview = ref(false)
const loggingOut = ref(false)
const isAdmin = computed(() => auth.user?.role === 'admin')

function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  try {
    localStorage.setItem(sidebarPreferenceKey, sidebarCollapsed.value ? '1' : '0')
  } catch {
    // 浏览器可能禁用存储,不影响导航。
  }
}

const menuGroups = computed(() => [
  {
    title: '工作空间',
    items: [
      { name: 'workbench', title: '工作台', path: '/', icon: 'grid' },
      { name: 'search', title: '搜索', path: '/search', icon: 'search' },
      { name: 'faults', title: '故障处理', path: '/faults', icon: 'activity' },
      { name: 'monitoring', title: '监控', path: '/monitoring', icon: 'server' },
      { name: 'chat', title: 'AI 对话', path: '/chat', icon: 'workflow' },
    ],
  },
  {
    title: '知识资产',
    items: [
      { name: 'cases', title: '案例库', path: '/cases', icon: 'files' },
      { name: 'sops', title: 'SOP 知识库', path: '/sops', icon: 'book' },
    ],
  },
  ...(isAdmin.value ? [{
    title: '管理',
    items: [{ name: 'admin', title: '系统管理', path: '/admin', icon: 'settings' }],
  }] : []),
])

const activeMenu = computed(() => {
  const name = String(route.name ?? '')
  const details: Record<string, string> = {
    'fault-detail': 'faults', 'case-detail': 'cases', 'sop-detail': 'sops',
  }
  return details[name] ?? name
})

const context = computed(() => {
  for (const group of menuGroups.value) {
    const current = group.items.find((item) => item.name === activeMenu.value)
    if (current) return { group: group.title, title: current.title }
  }
  return { group: '工作空间', title: '工作台' }
})

const aiModeLabel = computed(() => {
  if (statusLoading.value) return '正在读取模型模式'
  if (!aiStatus.value) return '模型模式未知'
  // 用户自填厂商配置时,即使全局未启用,该用户实际已可用
  if (aiStatus.value.user_ai_enabled) {
    const names: Record<string, string> = { deepseek: 'DeepSeek', minimax: 'MiniMax', mimo: '小米 MiMo' }
    return `AI 已启用 · ${names[aiStatus.value.user_provider] || aiStatus.value.user_provider}`
  }
  // 已保存 Key 但用户关闭了总开关
  if (aiStatus.value.user_ai_configured) return 'AI 已关闭'
  const labels: Record<string, string> = {
    disabled: 'AI 未启用', demo: 'AI 演示模式 · 模拟数据',
    cloud: 'AI 云端模式', local: 'AI 本地模式',
  }
  return labels[aiStatus.value.ai_mode] ?? '模型模式未知'
})

const userInitial = computed(() => auth.user?.display_name?.trim().slice(0, 1) || 'U')
const accountSubtitle = computed(
  () => auth.user?.signature?.trim() || (auth.user ? roleMap[auth.user.role] : ''),
)
watch(() => route.fullPath, () => {
  mobileMenuOpen.value = false
  // 内容区独立滚动:切换页面后从顶部开始,避免沿用上一页的滚动位置
  mainEl.value?.scrollTo({ top: 0 })
})
// 角色以后端为准:被降级或停用后不再保留管理员入口
watch(
  () => auth.user?.role,
  (role) => {
    if (route.meta.requiresAdmin && role !== 'admin') {
      void router.replace({ name: 'workbench' })
    }
  },
)

onMounted(async () => {
  try {
    const { data } = await client.get('/api/system/ai-status')
    aiStatus.value = data
  } catch {
    aiStatus.value = null
  } finally {
    statusLoading.value = false
  }
  try {
    // 每次进入工作区向后端复核资料/角色,避免本地缓存里的旧角色长期生效
    const { data } = await client.get<User>('/api/auth/me')
    auth.updateUser(data)
    if (route.meta.requiresAdmin && data.role !== 'admin') {
      await router.replace({ name: 'workbench' })
    }
  } catch {
    // 401 由全局拦截器处理;网络异常时保留当前会话,不阻断页面
  }
})

async function logout() {
  if (loggingOut.value) return
  loggingOut.value = true
  try {
    await client.post('/api/auth/logout')
    auth.logout()
    await router.replace({ name: 'login' })
  } catch (e) {
    ElMessage.error('退出失败，请重试：' + errMsg(e))
  } finally {
    loggingOut.value = false
  }
}

</script>

<template>
  <div class="ops-layout">
    <button v-if="mobileMenuOpen" class="ops-sidebar-scrim" aria-label="关闭导航菜单" @click="mobileMenuOpen = false"></button>
    <aside id="ops-navigation" class="ops-sidebar" :class="{ 'is-open': mobileMenuOpen, 'is-collapsed': sidebarCollapsed }">
      <router-link to="/" class="ops-brand-link" aria-label="OpsSOP 工作台">
        <AppLogo class="ops-brand-full" />
        <AppLogo icon-only class="ops-brand-compact" />
        <span class="ops-brand-caption">运维知识工作空间</span>
      </router-link>

      <nav class="ops-navigation" aria-label="主导航">
        <div v-for="group in menuGroups" :key="group.title" class="ops-nav-group">
          <div class="ops-nav-caption">{{ group.title }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.name"
            :to="item.path"
            class="ops-nav-link"
            :class="{ 'is-active': activeMenu === item.name }"
            :aria-current="activeMenu === item.name ? 'page' : undefined"
            :aria-label="item.title"
            :title="item.title"
          >
            <AppIcon :name="item.icon" :size="19" />
            <span class="ops-nav-label">{{ item.title }}</span>
            <span v-if="activeMenu === item.name" class="ops-nav-active-mark"></span>
          </router-link>
        </div>
      </nav>

      <div class="ops-sidebar-footer">
        <div class="ops-sidebar-mode" :class="{ 'is-demo': aiStatus?.ai_mode === 'demo' }" role="status" :aria-label="aiModeLabel" :title="aiModeLabel">
          <AppIcon name="bot" :size="17" /><span>{{ aiModeLabel }}</span>
        </div>
        <div class="ops-account">
          <button type="button" class="ops-account-main" aria-label="个人资料" title="个人资料：头像、签名与密码" @click="profileDialog = true">
            <span class="ops-avatar" aria-hidden="true">
              <img v-if="auth.user?.avatar" :src="auth.user.avatar" alt="" />
              <template v-else>{{ userInitial }}</template>
            </span>
            <span class="ops-account-copy">
              <strong>{{ auth.user?.display_name }}</strong>
              <span :title="accountSubtitle">{{ accountSubtitle }}</span>
            </span>
            <AppIcon name="chevron-right" :size="14" class="ops-account-chevron" />
          </button>
          <button class="ops-logout-button" title="退出登录" aria-label="退出登录" :disabled="loggingOut" @click="logout">
            <AppIcon name="logout" :size="18" />
          </button>
        </div>
      </div>
    </aside>

    <ProfileDialog v-model="profileDialog" />
    <FeedbackPreview v-model="feedbackPreview" />

    <div class="ops-workspace">
      <SignalBackdrop />
      <header class="ops-topbar">
        <div class="ops-topbar-location">
          <button
            type="button"
            class="ops-sidebar-toggle"
            :aria-label="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
            :title="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
            aria-controls="ops-navigation"
            :aria-expanded="!sidebarCollapsed"
            @click="toggleSidebar"
          ><AppIcon :name="sidebarCollapsed ? 'sidebar-expand' : 'sidebar-collapse'" :size="20" /></button>
          <button
            class="ops-mobile-menu-button"
            aria-label="打开导航菜单"
            aria-controls="ops-navigation"
            :aria-expanded="mobileMenuOpen"
            @click="mobileMenuOpen = !mobileMenuOpen"
          ><AppIcon :name="mobileMenuOpen ? 'close' : 'menu'" :size="21" /></button>
          <span class="ops-breadcrumb-parent">{{ context.group }}</span>
          <AppIcon name="chevron-right" :size="14" class="ops-breadcrumb-divider" />
          <span class="ops-breadcrumb-current">{{ context.title }}</span>
        </div>
        <div class="ops-topbar-actions">
          <el-button class="ops-preview-button" text @click="feedbackPreview = true"><AppIcon name="workflow" :size="16" /><span>动效预览</span></el-button>
          <NotificationBell />
        </div>
      </header>
      <main ref="mainEl" class="ops-main" tabindex="-1">
        <router-view v-slot="{ Component }">
          <Transition name="ops-page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.ops-layout { display: flex; height: 100%; min-height: 0; color: var(--ops-text); }
.ops-sidebar { border-right: 1px solid var(--ops-border); transition: width 280ms ease, flex-basis 280ms ease; display: flex; flex-direction: column; flex: 0 0 var(--ops-sidebar-width); width: var(--ops-sidebar-width); background: var(--ops-sidebar); z-index: 40; }
.ops-brand-link { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; min-height: 100px; padding: 26px 24px; color: var(--ops-text); text-decoration: none; }
.ops-brand-caption { margin-left: 66px; color: var(--ops-sidebar-muted); font-size: 9px; letter-spacing: 1px; white-space: nowrap; }
.ops-brand-compact { display: none; }
.ops-navigation { flex: 1; overflow-y: auto; padding: 6px 16px 24px; }
.ops-nav-group + .ops-nav-group { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--ops-border-light); }
.ops-nav-caption { margin: 0 12px 10px; color: var(--ops-sidebar-muted); font-size: 12px; letter-spacing: 0; }
.ops-nav-link { position: relative; display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 0 13px; margin: 4px 0; border-radius: 8px; color: var(--ops-sidebar-text); font-size: 13px; text-decoration: none; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard), color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-nav-link > svg { color: var(--ops-sidebar-muted); transition: color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-nav-link::after { content: ''; position: absolute; left: 13px; right: 13px; bottom: 0; height: 1px; background: var(--ops-brand-line); transform: scaleX(0); transform-origin: left; transition: transform var(--ops-duration-base); }
.ops-nav-link:hover::after { transform: scaleX(1); }
.ops-nav-link:hover { background: var(--ops-surface-warm); color: var(--ops-text); }
.ops-nav-link:hover > svg { color: var(--ops-brand-mark); }
.ops-nav-link.is-active { background: var(--ops-paper-active); color: var(--ops-brand); font-weight: 600; box-shadow: inset 3px 0 var(--ops-brand-mark); }
.ops-nav-link.is-active > svg { color: var(--ops-brand); }
.ops-nav-active-mark { width: 6px; height: 6px; margin-left: auto; background: var(--ops-brand); transform: rotate(45deg); animation: ops-nav-marker-in var(--ops-duration-base) var(--ops-ease-out) both; }
@keyframes ops-nav-marker-in { from { opacity: 0; transform: rotate(45deg) scale(0.4); } to { opacity: 1; transform: rotate(45deg) scale(1); } }
.ops-sidebar-footer { padding: 16px; }
.ops-sidebar-mode { display: flex; align-items: center; gap: 8px; margin-bottom: 17px; padding: 11px 12px; border: 1px solid var(--ops-sidebar-line); border-radius: 8px; background: var(--ops-sidebar-raised); color: var(--ops-sidebar-muted); font-size: 10px; line-height: 1.5; }
.ops-sidebar-mode.is-demo { color: var(--ops-warning); border-color: var(--ops-warning-line); }
.ops-account { display: flex; align-items: center; gap: 4px; padding: 16px 5px 2px; border-top: 1px solid var(--ops-sidebar-line); }
.ops-account-main { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; padding: 4px 5px; border: 0; border-radius: 5px; background: transparent; color: inherit; text-align: left; cursor: pointer; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-account-main:hover { background: var(--ops-surface-warm); }
.ops-account-main:focus-visible { outline: 2px solid var(--ops-brand-mark); outline-offset: 1px; }
.ops-account-chevron { flex-shrink: 0; color: var(--ops-sidebar-muted); opacity: 0.7; }
.ops-avatar { display: grid; place-items: center; width: 34px; height: 34px; flex-shrink: 0; overflow: hidden; border: 1px solid var(--ops-border); border-radius: 5px; background: var(--ops-surface-warm); color: var(--ops-brand); font-size: 12px; font-weight: 600; }
.ops-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ops-account-copy { min-width: 0; flex: 1; }
.ops-account-copy strong, .ops-account-copy span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops-account-copy strong { color: var(--ops-text); font-size: 12px; font-weight: 600; }
.ops-account-copy span { margin-top: 3px; color: var(--ops-sidebar-muted); font-size: 10px; }
.ops-logout-button, .ops-mobile-menu-button, .ops-sidebar-toggle { display: grid; place-items: center; width: 32px; height: 32px; flex-shrink: 0; border: 0; border-radius: 8px; background: transparent; color: var(--ops-sidebar-muted); cursor: pointer; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard), color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-logout-button:hover { background: var(--ops-surface-warm); color: var(--ops-brand); }
.ops-mobile-menu-button, .ops-sidebar-toggle { color: var(--ops-muted); }
.ops-mobile-menu-button:hover, .ops-sidebar-toggle:hover { background: var(--ops-surface-warm); color: var(--ops-text); }
.ops-workspace { position: relative; isolation: isolate; overflow: hidden; display: flex; flex-direction: column; flex: 1; min-width: 0; min-height: 0; }
.ops-topbar { position: relative; z-index: 2; display: flex; align-items: center; justify-content: space-between; gap: 20px; height: var(--ops-header-height); min-height: var(--ops-header-height); padding: 0 var(--ops-page-gutter); border-bottom: 1px solid var(--ops-border); background: var(--ops-surface); }
.ops-topbar-location, .ops-topbar-actions { display: flex; align-items: center; }
.ops-topbar-actions { gap: 6px; }
.ops-topbar-location { gap: 12px; font-size: 12px; }
.ops-breadcrumb-parent, .ops-breadcrumb-divider { color: var(--ops-muted); }
.ops-breadcrumb-current { color: var(--ops-text); font-weight: 600; }
.ops-main { position: relative; z-index: 1; flex: 1; min-width: 0; min-height: 0; overflow: auto; background: transparent; }
.ops-mobile-menu-button, .ops-sidebar-scrim { display: none; }
@media (min-width: 901px) {
  .ops-sidebar.is-collapsed { flex-basis: 76px; width: 76px; }
  .is-collapsed .ops-brand-link { align-items: center; justify-content: center; min-height: 100px; padding: 24px 12px; }
  .is-collapsed .ops-brand-full, .is-collapsed .ops-brand-caption,
  .is-collapsed .ops-nav-caption, .is-collapsed .ops-nav-label,
  .is-collapsed .ops-nav-active-mark, .is-collapsed .ops-sidebar-mode > span,
  .is-collapsed .ops-account-copy, .is-collapsed .ops-account-chevron { display: none; }
  .is-collapsed .ops-brand-compact { display: block; }
  .is-collapsed .ops-navigation { padding-right: 12px; padding-left: 12px; }
  .is-collapsed .ops-nav-link { justify-content: center; padding: 0; }
  .is-collapsed .ops-nav-group + .ops-nav-group { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--ops-sidebar-line); }
  .is-collapsed .ops-sidebar-footer { padding: 12px; }
  .is-collapsed .ops-sidebar-mode { justify-content: center; margin-bottom: 12px; padding: 12px 0; }
  .is-collapsed .ops-account { flex-direction: column; gap: 8px; padding-right: 0; padding-left: 0; }
  .is-collapsed .ops-account-main { flex: none; justify-content: center; width: 44px; padding: 5px; }
  .is-collapsed .ops-logout-button { width: 44px; height: 36px; }
}
@media (max-width: 900px) {
  .ops-sidebar-toggle { display: none; }
  .ops-sidebar { position: fixed; inset: 0 auto 0 0; visibility: hidden; transform: translateX(-100%); transition: transform var(--ops-duration-panel) var(--ops-ease-out), visibility 0s var(--ops-duration-panel); }
  .ops-sidebar.is-open { visibility: visible; transform: translateX(0); transition-delay: 0s; box-shadow: 24px 0 60px rgb(20 21 24 / 12%); }
  .ops-mobile-menu-button { display: grid; }
  .ops-sidebar-scrim { display: block; position: fixed; inset: 0; z-index: 35; border: 0; background: rgb(20 21 24 / 38%); animation: ops-fade-in var(--ops-duration-base) var(--ops-ease-standard) both; }
  .ops-topbar { padding: 0 20px; }
}
@keyframes ops-fade-in { from { opacity: 0; } to { opacity: 1; } }
@media (max-width: 560px) { .ops-topbar-location { gap: 8px; } }
.ops-preview-button :deep(> span) { gap: 7px; }
.ops-preview-button { color: var(--ops-brand); }
@media (max-width: 560px) { .ops-breadcrumb-parent, .ops-breadcrumb-divider { display: none; } .ops-topbar { gap: 8px; } }
</style>



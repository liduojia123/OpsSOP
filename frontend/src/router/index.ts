import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    {
      path: '/',
      component: () => import('../views/AppLayout.vue'),
      children: [
        { path: '', name: 'workbench', component: () => import('../views/WorkbenchView.vue') },
        { path: 'search', name: 'search', component: () => import('../views/SearchView.vue') },
        { path: 'faults', name: 'faults', component: () => import('../views/FaultListView.vue') },
        { path: 'faults/:id', name: 'fault-detail', component: () => import('../views/FaultDetailView.vue') },
        { path: 'cases', name: 'cases', component: () => import('../views/CaseListView.vue') },
        {
          // 来源定位:直接按 ID 打开案例,支持刷新/书签/前后退;不依赖列表筛选或缓存
          path: 'cases/:id',
          name: 'case-detail',
          component: () => import('../views/CaseDetailView.vue'),
        },
        { path: 'sops', name: 'sops', component: () => import('../views/SopListView.vue') },
        { path: 'ai-settings', name: 'ai-settings', component: () => import('../views/AiSettingsView.vue') },
        { path: 'monitoring', name: 'monitoring', component: () => import('../views/MonitoringView.vue') },
        { path: 'chat', name: 'chat', component: () => import('../views/ChatView.vue') },
        { path: 'sops/:id', name: 'sop-detail', component: () => import('../views/SopDetailView.vue') },
        {
          path: 'admin',
          name: 'admin',
          component: () => import('../views/AdminView.vue'),
          meta: { requiresAdmin: true },
        },
      ],
    },
  ],
})

// 路由 meta 声明权限;页面内仍会再校验一次,后端接口始终 require_admin
declare module 'vue-router' {
  interface RouteMeta {
    requiresAdmin?: boolean
  }
}

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) {
    // 未登录直接打开来源(如 /cases/12、/sops/3)时,登录后回到同一来源
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.requiresAdmin && auth.role !== 'admin') {
    return { name: 'workbench' }
  }
  return true
})

export default router

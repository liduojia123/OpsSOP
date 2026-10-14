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
        { path: 'faults', name: 'faults', component: () => import('../views/FaultListView.vue') },
        { path: 'faults/:id', name: 'fault-detail', component: () => import('../views/FaultDetailView.vue') },
        { path: 'cases', name: 'cases', component: () => import('../views/CaseListView.vue') },
        { path: 'sops', name: 'sops', component: () => import('../views/SopListView.vue') },
        { path: 'sops/:id', name: 'sop-detail', component: () => import('../views/SopDetailView.vue') },
        { path: 'admin', name: 'admin', component: () => import('../views/AdminView.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) {
    return { name: 'login' }
  }
  if (to.name === 'admin' && auth.role !== 'admin') {
    return { name: 'workbench' }
  }
  return true
})

export default router

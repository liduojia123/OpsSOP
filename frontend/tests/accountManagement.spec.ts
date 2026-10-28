import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn() },
  auth: { token: 'session', user: { id: 1, display_name: 'User', role: 'operator' }, role: 'operator', logout: vi.fn() },
  router: { replace: vi.fn(), push: vi.fn() },
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))
vi.mock('../src/api', () => ({ default: mocks.api, errMsg: (e: Error) => e.message }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('vue-router', () => ({ useRoute: () => ({ name: 'workbench', fullPath: '/' }), useRouter: () => mocks.router }))
vi.mock('element-plus', () => ({ ElMessage: mocks.message, ElMessageBox: {} }))

import AppLayout from '../src/views/AppLayout.vue'
import AdminView from '../src/views/AdminView.vue'

function setupState(wrapper: ReturnType<typeof mount>) {
  return (wrapper.vm.$ as unknown as { setupState: any }).setupState
}

const stubs = Object.fromEntries([
  'RouterLink', 'RouterView', 'el-button', 'el-dialog', 'el-form', 'el-form-item',
  'el-input', 'el-input-number', 'el-select', 'el-option', 'el-date-picker',
  'el-tabs', 'el-tab-pane', 'el-table', 'el-table-column', 'el-tag', 'el-alert',
].map((name) => [name, true]))
const global = { stubs, directives: { loading: {} } }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.api.get.mockImplementation((url: string) => {
    if (url === '/api/users' || url === '/api/categories') return Promise.resolve({ data: [] })
    return Promise.resolve({ data: { ai_mode: 'disabled' } })
  })
  mocks.api.post.mockResolvedValue({ data: null })
  mocks.router.replace.mockResolvedValue(undefined)
})

describe('account actions', () => {
  it('revokes the current session before clearing it locally', async () => {
    const wrapper = mount(AppLayout, { global })
    await flushPromises()
    await setupState(wrapper).logout()
    expect(mocks.api.post).toHaveBeenCalledWith('/api/auth/logout')
    expect(mocks.auth.logout).toHaveBeenCalledOnce()
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'login' })
  })

  it('keeps the local session if server logout fails', async () => {
    mocks.api.post.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(AppLayout, { global })
    await flushPromises()
    await setupState(wrapper).logout()
    expect(mocks.auth.logout).not.toHaveBeenCalled()
    expect(mocks.message.error).toHaveBeenCalled()
  })

  it('submits password change and requires a new login', async () => {
    const wrapper = mount(AppLayout, { global })
    await flushPromises()
    const state = setupState(wrapper)
    Object.assign(state.passwordForm, { current: 'old-password', next: 'new-password', confirm: 'new-password' })
    await state.changePassword()
    expect(mocks.api.post).toHaveBeenCalledWith('/api/auth/change-password', { current_password: 'old-password', new_password: 'new-password' })
    expect(mocks.auth.logout).toHaveBeenCalledOnce()
    expect(state.passwordForm.current).toBe('')
  })

  it('keeps applied audit filters while paging', async () => {
    const wrapper = mount(AdminView, { global })
    await flushPromises()
    const state = setupState(wrapper)
    state.auditAction = 'user.update'
    mocks.api.get.mockResolvedValueOnce({ data: Array.from({ length: 51 }, (_, i) => ({ id: 100 - i })) })
    await state.loadAudit()
    expect(state.auditHasMore).toBe(true)
    state.auditAction = 'other'
    mocks.api.get.mockResolvedValueOnce({ data: [] })
    await state.loadAudit('next')
    expect(mocks.api.get).toHaveBeenLastCalledWith('/api/audit', { params: { action: 'user.update', limit: 51, before_id: 51 } })
  })
})

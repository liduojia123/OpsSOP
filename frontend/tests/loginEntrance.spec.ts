import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({ post: vi.fn(), replace: vi.fn(), setSession: vi.fn(), error: vi.fn() }))
vi.mock('../src/api', () => ({ default: { post: mocks.post }, errMsg: String }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => ({ setSession: mocks.setSession }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: { redirect: '/cases/12' } }), useRouter: () => ({ replace: mocks.replace }) }))
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: mocks.error } }))
import LoginView from '../src/views/LoginView.vue'
import EnergyProgress from '../src/components/EnergyProgress.vue'
import { workspaceEntrance, cancelWorkspaceEntrance } from '../src/workspaceEntrance'

const wrappers: ReturnType<typeof shallowMount>[] = []
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => { if (wrapper.exists()) wrapper.unmount() })
  cancelWorkspaceEntrance()
  vi.resetAllMocks()
  vi.useRealTimers()
})
function login() {
  const wrapper = shallowMount(LoginView)
  wrappers.push(wrapper)
  const vm = wrapper.vm as any
  vm.form.username = 'test'
  vm.form.password = 'test-password'
  return { wrapper, vm }
}

it('验证成功才覆盖画面,目标路由就绪后开门,保留登录回跳', async () => {
  vi.useFakeTimers()
  let authenticate!: (value: unknown) => void
  let navigate!: (value?: unknown) => void
  mocks.post.mockReturnValueOnce(new Promise(resolve => { authenticate = resolve }))
  mocks.replace.mockReturnValueOnce(new Promise(resolve => { navigate = resolve }))
  const { wrapper, vm } = login()
  const request = vm.doLogin()
  await vm.doLogin()
  expect(mocks.post).toHaveBeenCalledTimes(1)
  expect(workspaceEntrance.phase).toBe('idle')
  authenticate({ data: { token: 'test-token', user: { display_name: '测试用户' } } })
  await flushPromises()
  expect(mocks.setSession).not.toHaveBeenCalled()
  expect(workspaceEntrance.phase).toBe('idle')
  expect(wrapper.find('energy-progress-stub').exists()).toBe(true)
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBe(0)
  await vi.advanceTimersByTimeAsync(1500)
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBeGreaterThanOrEqual(49)
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBeLessThanOrEqual(50)
  await vi.advanceTimersByTimeAsync(1499)
  expect(workspaceEntrance.phase).toBe('idle')
  expect(mocks.replace).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBe(100)
  expect(workspaceEntrance.phase).toBe('idle')
  expect(mocks.replace).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(150)
  expect(mocks.setSession).toHaveBeenCalled()
  expect(workspaceEntrance.phase).toBe('covered')
  expect(mocks.replace).toHaveBeenCalledWith('/cases/12')
  navigate()
  await request
  expect(workspaceEntrance.phase).toBe('opening')
  wrapper.unmount()
})

it('密码错误不播放开门,路由失败会移除遮罩并允许重试', async () => {
  vi.useFakeTimers()
  const { wrapper, vm } = login()
  mocks.post.mockRejectedValueOnce(new Error('密码错误'))
  await vm.doLogin()
  expect(workspaceEntrance.phase).toBe('idle')
  expect(mocks.replace).not.toHaveBeenCalled()
  mocks.post.mockResolvedValueOnce({ data: { token: 'test-token', user: { display_name: '测试用户' } } })
  mocks.replace.mockRejectedValueOnce(new Error('页面加载失败'))
  const retry = vm.doLogin()
  await vi.advanceTimersByTimeAsync(3150)
  await retry
  expect(workspaceEntrance.phase).toBe('idle')
  expect(vm.loading).toBe(false)
  expect(mocks.error).toHaveBeenCalled()
  wrapper.unmount()
})

it('验证超过三秒时不额外等待,提前离开登录页不继续入场', async () => {
  vi.useFakeTimers()
  let authenticate!: (value: unknown) => void
  mocks.post.mockReturnValueOnce(new Promise(resolve => { authenticate = resolve }))
  mocks.replace.mockResolvedValueOnce(undefined)
  const { wrapper, vm } = login()
  const request = vm.doLogin()
  await vi.advanceTimersByTimeAsync(3500)
  expect(workspaceEntrance.phase).toBe('idle')
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBe(99)
  authenticate({ data: { token: 'test-token', user: { display_name: '测试用户' } } })
  await flushPromises()
  expect(wrapper.findComponent(EnergyProgress).props('progress')).toBe(100)
  await vi.advanceTimersByTimeAsync(150)
  await request
  expect(workspaceEntrance.phase).toBe('opening')
  wrapper.unmount()
  cancelWorkspaceEntrance()
  mocks.setSession.mockClear()
  mocks.replace.mockClear()
  mocks.post.mockResolvedValueOnce({ data: { token: 'test-token', user: { display_name: '测试用户' } } })
  const next = login()
  const abandoned = next.vm.doLogin()
  await flushPromises()
  next.wrapper.unmount()
  await abandoned
  await vi.advanceTimersByTimeAsync(3500)
  expect(workspaceEntrance.phase).toBe('idle')
  expect(mocks.setSession).not.toHaveBeenCalled()
  expect(mocks.replace).not.toHaveBeenCalled()
})

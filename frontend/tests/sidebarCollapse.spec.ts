import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  auth: { user: { id: 1, display_name: '测试用户', role: 'operator' }, updateUser: vi.fn() },
}))
vi.mock('../src/api', () => ({ default: { get: mocks.get }, errMsg: String }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ name: 'workbench', fullPath: '/', meta: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}))
import AppLayout from '../src/views/AppLayout.vue'

const key = 'opssop_sidebar_collapsed'
const wrappers: ReturnType<typeof shallowMount>[] = []
function layout() {
  const wrapper = shallowMount(AppLayout, { global: { stubs: {
    RouterLink: defineComponent({ props: ['to'], setup(props, { slots }) { return () => h('a', { href: props.to }, slots.default?.()) } }),
    RouterView: true,
    ElButton: true,
  } } })
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  localStorage.removeItem(key)
  mocks.get.mockImplementation((url: string) => Promise.resolve({ data: url === '/api/auth/me' ? mocks.auth.user : { ai_mode: 'disabled' } }))
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.restoreAllMocks()
  localStorage.removeItem(key)
})

describe('侧边栏收起', () => {
  it('切换为图标栏并记住选择,导航保持名称与当前页标记', async () => {
    const wrapper = layout()
    await flushPromises()
    const toggle = wrapper.get('button[aria-label="收起侧边栏"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    expect(wrapper.get('.ops-sidebar').classes()).toContain('is-collapsed')
    expect(wrapper.get('button[aria-label="展开侧边栏"]').attributes('aria-expanded')).toBe('false')
    expect(localStorage.getItem(key)).toBe('1')
    const links = wrapper.findAll('.ops-nav-link')
    expect(links.length).toBe(7)
    for (const link of links) {
      expect(link.attributes('aria-label')).toBeTruthy()
      expect(link.attributes('title')).toBe(link.attributes('aria-label'))
    }
    expect(wrapper.get('.ops-nav-link[aria-current="page"]').attributes('href')).toBe('/')
  })

  it('重新打开布局恢复收起状态,可以展开并打开个人资料', async () => {
    localStorage.setItem(key, '1')
    const wrapper = layout()
    await flushPromises()
    expect(wrapper.get('.ops-sidebar').classes()).toContain('is-collapsed')
    await wrapper.get('.ops-account-main').trigger('click')
    expect((wrapper.vm as any).profileDialog).toBe(true)
    await wrapper.get('button[aria-label="展开侧边栏"]').trigger('click')
    expect(wrapper.get('.ops-sidebar').classes()).not.toContain('is-collapsed')
    expect(localStorage.getItem(key)).toBe('0')
  })

  it('浏览器禁止存储时仍可切换侧边栏', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    const wrapper = layout()
    await flushPromises()
    await wrapper.get('button[aria-label="收起侧边栏"]').trigger('click')
    expect(wrapper.get('.ops-sidebar').classes()).toContain('is-collapsed')
  })
})

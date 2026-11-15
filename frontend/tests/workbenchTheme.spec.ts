import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const mocks = vi.hoisted(() => ({ api: { get: vi.fn() } }))
vi.mock('../src/api', () => ({ default: mocks.api, errMsg: (e: Error) => e.message }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => ({ role: 'operator' }) }))

import WorkbenchView from '../src/views/WorkbenchView.vue'
import { DEFAULT_THEME, applyTheme, setTheme } from '../src/theme'

const stubs = {
  'router-link': defineComponent({
    props: { to: { type: [String, Object], required: true } },
    setup(_: unknown, { slots }: any) {
      return () => h('a', slots.default?.())
    },
  }),
  'el-tag': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('span', slots.default?.())
    },
  }),
  'el-alert': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('div', slots.default?.())
    },
  }),
  'el-button': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('button', slots.default?.())
    },
  }),
}

function mountWorkbench() {
  return mount(WorkbenchView, { global: { stubs } })
}

beforeEach(() => {
  applyTheme(DEFAULT_THEME)
  mocks.api.get.mockReset()
  mocks.api.get.mockResolvedValue({
    data: {
      my_open_faults: [],
      pending_case_reviews: [],
      pending_sop_reviews: [],
      recent_sops: [],
      published_sop_count: 0,
      category_stats: [],
      my_returned_cases: [],
      my_returned_sops: [],
    },
  })
})

describe('工作台标题区', () => {
  it('经典档案主题补充 3.6 的深色标题区文案与装饰，默认主题保持简洁页头', async () => {
    const wrapper = mountWorkbench()
    await flushPromises()
    expect(wrapper.get('.ops-dashboard .ops-page-header h1').text()).toBe('工作台')
    expect(wrapper.find('.signal-art').exists()).toBe(false)

    setTheme('classic')
    await flushPromises()
    expect(wrapper.get('.ops-dashboard .ops-page-header h1').text()).toContain('故障有解')
    // 装饰图形只标记为 aria-hidden，不进入可访问名称
    expect(wrapper.find('.signal-art').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('.ops-dashboard .ops-page-header h1').attributes('aria-label')).toBe('工作台')

    setTheme('signal')
    await flushPromises()
    expect(wrapper.get('.ops-dashboard .ops-page-header h1').text()).toBe('工作台')
    wrapper.unmount()
  })
})

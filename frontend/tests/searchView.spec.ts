import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const routerState = vi.hoisted(() => ({
  route: { query: {} as Record<string, string> },
  replace: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => routerState.route,
  useRouter: () => ({ replace: routerState.replace }),
}))

const client = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../src/api', () => ({
  default: client,
  errMsg: (e: unknown) => String(e),
}))

import SearchView from '../src/views/SearchView.vue'

const stubs = {
  'el-input': defineComponent({
    props: { modelValue: { type: String, default: '' } },
    emits: ['update:modelValue', 'keyup'],
    setup(props: { modelValue: string }, { emit }: any) {
      return () =>
        h('input', {
          value: props.modelValue,
          onInput: (event: Event) =>
            emit('update:modelValue', (event.target as HTMLInputElement).value),
          onKeyup: (event: KeyboardEvent) => emit('keyup', event),
        })
    },
  }),
  'el-button': defineComponent({
    emits: ['click'],
    setup(_: unknown, { slots, emit }: any) {
      return () => h('button', { onClick: () => emit('click') }, slots.default?.())
    },
  }),
  'el-alert': defineComponent({
    props: { title: String },
    setup(props: { title?: string }) {
      return () => h('div', { class: 'alert' }, props.title)
    },
  }),
  'el-tag': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('span', { class: 'tag' }, slots.default?.())
    },
  }),
  'router-link': defineComponent({
    props: { to: { type: [String, Object], required: true } },
    setup(props: { to: string | Record<string, unknown> }, { slots }: any) {
      return () => h('a', { href: String(props.to) }, slots.default?.())
    },
  }),
}

function hit(overrides: Record<string, unknown>) {
  return {
    status: 'open',
    subtitle: '',
    updated_at: '2026-10-30T00:00:00Z',
    ...overrides,
  }
}

describe('SearchView', () => {
  beforeEach(() => {
    client.get.mockReset()
    routerState.replace.mockReset()
  })

  it('按关键词搜索并按类型分组生成详情链接', async () => {
    client.get.mockResolvedValue({
      data: {
        query: '磁盘',
        groups: [
          {
            type: 'fault',
            total: 1,
            truncated: false,
            items: [
              hit({ type: 'fault', id: 5, title: '磁盘写满', link: '/faults/5' }),
            ],
          },
          { type: 'case', total: 0, truncated: false, items: [] },
          {
            type: 'sop',
            total: 1,
            truncated: true,
            items: [
              hit({ type: 'sop', id: 2, title: '磁盘处置 SOP', link: '/sops/2' }),
            ],
          },
        ],
      },
    })

    const wrapper = mount(SearchView, { global: { components: stubs } })
    await wrapper.find('input').setValue('磁盘')
    const searchButton = wrapper.findAll('button').find((button) => button.text().includes('搜索'))
    expect(searchButton).toBeTruthy()
    await searchButton!.trigger('click')
    await flushPromises()

    expect(client.get).toHaveBeenCalledWith('/api/search/all', {
      params: { q: '磁盘', types: 'fault,case,sop', limit: 8 },
    })
    expect(wrapper.text()).toContain('磁盘写满')
    expect(wrapper.text()).toContain('磁盘处置 SOP')
    expect(wrapper.text()).toContain('仅显示前 1 条')
    expect(wrapper.findAll('a').map((link) => link.attributes('href'))).toEqual([
      '/faults/5',
      '/sops/2',
    ])
    expect(routerState.replace).toHaveBeenCalledWith({ path: '/search', query: { q: '磁盘' } })
  })

  it('初始状态提供关键词示例,点击后直接搜索', async () => {
    client.get.mockResolvedValue({
      data: { query: 'OOM', groups: [{ type: 'fault', total: 0, truncated: false, items: [] }] },
    })
    const wrapper = mount(SearchView, { global: { components: stubs } })
    expect(wrapper.text()).toContain('从一个关键词开始')

    const example = wrapper.findAll('button').find((button) => button.text() === 'OOM')
    expect(example).toBeTruthy()
    await example!.trigger('click')
    await flushPromises()

    expect(client.get).toHaveBeenCalledWith('/api/search/all', {
      params: { q: 'OOM', types: 'fault,case,sop', limit: 8 },
    })
    expect(wrapper.text()).toContain('没有匹配结果')
  })

  it('至少保留一种类型筛选', async () => {
    const wrapper = mount(SearchView, { global: { components: stubs } })
    const typeButtons = wrapper.findAll('button.search-type')
    expect(typeButtons).toHaveLength(3)
    await typeButtons[0].trigger('click')
    await typeButtons[1].trigger('click')
    // 只剩 SOP 时,再点击最后一个类型不应清空
    const active = wrapper.findAll('button.search-type.is-active')
    expect(active).toHaveLength(1)
    await active[0].trigger('click')
    expect(wrapper.findAll('button.search-type.is-active')).toHaveLength(1)
  })
})
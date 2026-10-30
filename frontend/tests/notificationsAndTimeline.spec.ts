import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, onMounted } from 'vue'

const routerState = vi.hoisted(() => ({
  push: vi.fn(),
  route: { fullPath: '/', query: {} as Record<string, string> },
}))
vi.mock('vue-router', () => ({
  useRoute: () => routerState.route,
  useRouter: () => ({ push: routerState.push }),
}))

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api', () => ({
  default: client,
  errMsg: (e: unknown) => String(e),
}))

import NotificationBell from '../src/components/NotificationBell.vue'
import AuditTimeline from '../src/components/AuditTimeline.vue'

const stubs = {
  'el-popover': defineComponent({
    emits: ['update:visible'],
    setup(_: unknown, { slots, emit }: any) {
      // 模拟用户打开下拉,触发受控 visible 事件
      onMounted(() => emit('update:visible', true))
      return () => h('div', { class: 'popover' }, [slots.reference?.(), slots.default?.()])
    },
  }),
}

describe('NotificationBell', () => {
  beforeEach(() => {
    client.get.mockReset()
    client.post.mockReset()
    routerState.push.mockReset()
  })

  it('显示未读数,点击通知标记已读并跳转', async () => {
    client.get.mockImplementation((url: string) => {
      if (url === '/api/notifications/unread-count') {
        return Promise.resolve({ data: { count: 1 } })
      }
      return Promise.resolve({
        data: [
          {
            id: 7,
            user_id: 1,
            kind: 'case.returned',
            title: '案例 #3 被退回',
            body: '请补充恢复验证',
            link: '/cases/3',
            entity_type: 'case',
            entity_id: 3,
            read_at: null,
            created_at: '2026-10-30T10:00:00Z',
          },
        ],
      })
    })
    client.post.mockResolvedValue({ data: { updated: 1 } })

    const wrapper = mount(NotificationBell, { global: { components: stubs } })
    await flushPromises()
    expect(wrapper.find('.ops-bell-badge').text()).toBe('1')

    // 展开列表(受控 visible):直接调用组件暴露的 open 状态不可行,改由条目渲染后点击
    const item = wrapper.find('.ops-bell-item')
    expect(item.exists()).toBe(true)
    await item.trigger('click')
    await flushPromises()

    expect(client.post).toHaveBeenCalledWith('/api/notifications/7/read')
    expect(routerState.push).toHaveBeenCalledWith('/cases/3')
  })
})

describe('AuditTimeline', () => {
  beforeEach(() => {
    client.get.mockReset()
  })

  it('展示动作标签、操作者与审核意见', async () => {
    client.get.mockResolvedValue({
      data: [
        {
          id: 2,
          action: 'case.return',
          actor_name: '审核员',
          detail: { comment: '请补充恢复验证' },
          created_at: '2026-10-30T10:00:00Z',
        },
        {
          id: 1,
          action: 'case.submit',
          actor_name: '运维',
          detail: {},
          created_at: '2026-10-30T09:00:00Z',
        },
      ],
    })
    const wrapper = mount(AuditTimeline, {
      props: { entityType: 'case', entityId: 3 },
    })
    await flushPromises()

    expect(client.get).toHaveBeenCalledWith('/api/audit/entity', {
      params: { entity_type: 'case', entity_id: 3, limit: 20 },
    })
    expect(wrapper.text()).toContain('审核退回')
    expect(wrapper.text()).toContain('审核员')
    expect(wrapper.text()).toContain('请补充恢复验证')
    expect(wrapper.text()).toContain('提交审核')
  })

  it('接口返回异常结构时不崩溃', async () => {
    client.get.mockResolvedValue({ data: { detail: 'forbidden' } })
    const wrapper = mount(AuditTimeline, {
      props: { entityType: 'fault', entityId: 1 },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('暂无操作记录')
  })
})
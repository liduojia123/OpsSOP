/**
 * 案例来源直达页(第四阶段第一批)。
 *
 * 覆盖:
 * - 直接按 ID 调用单例接口,不依赖列表;
 * - 403/404/非法 ID/网络错误清晰展示,失败不残留上一个来源正文;
 * - 快速切换 A/B 时迟到响应不覆盖新记录。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, reactive } from 'vue'

const routerState = vi.hoisted(() => ({
  route: { params: { id: '7' } as Record<string, string> },
  push: vi.fn(),
  back: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => routerState.route,
  useRouter: () => ({ push: routerState.push, back: routerState.back }),
}))

const client = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../src/api', () => ({
  default: client,
  errMsg: (e: any) => e?.message ?? String(e),
}))

import CaseDetailView from '../src/views/CaseDetailView.vue'

// 路由参数需要响应式,组件才能监听同页 A/B 切换
routerState.route = reactive(routerState.route)

const globalCfg = {
  components: {
    'el-button': defineComponent({
      props: { disabled: Boolean, loading: Boolean },
      emits: ['click'],
      setup(props: { disabled: boolean; loading: boolean }, { slots, emit }: any) {
        return () =>
          h(
            'button',
            {
              type: 'button',
              disabled: props.disabled || props.loading,
              onClick: () => emit('click'),
            },
            slots.default?.(),
          )
      },
    }),
    'el-tag': defineComponent({
      setup(_: unknown, { slots }: any) {
        return () => h('span', { class: 'tag' }, slots.default?.())
      },
    }),
    'el-alert': defineComponent({
      props: { title: String, description: String },
      setup(props: { title?: string; description?: string }) {
        return () =>
          h('div', { class: 'alert' }, [props.title, props.description].filter(Boolean).join(' | '))
      },
    }),
  },
}

function caseData(id: number, title: string, status = 'approved') {
  return {
    id,
    fault_id: 100,
    title,
    symptom: 'checkout 服务 OOMKilled',
    environment: 'prod',
    service: 'checkout',
    component_version: '1.8.2',
    category_id: 1,
    tags: ['演示数据'],
    confirmed_root_cause: 'limits.memory 过低',
    handling_steps: '调整 limits',
    recovery_verification: '稳定运行',
    status,
    review_comment: '',
    reviewed_by: 2,
    reviewed_at: '2026-10-21T00:00:00Z',
    created_by: 1,
    created_at: '2026-10-21T00:00:00Z',
    updated_at: '2026-10-21T00:00:00Z',
  }
}

function axiosError(status?: number, message = '请求失败') {
  return { isAxiosError: true, response: status ? { status } : undefined, message }
}

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  client.get.mockReset()
  routerState.push.mockReset()
  routerState.back.mockReset()
  routerState.route.params.id = '7'
})

describe('CaseDetailView 来源直达与错误状态', () => {
  it('直接按 ID 加载单例,不依赖列表/筛选', async () => {
    client.get.mockResolvedValue({ data: caseData(7, '来源案例标题') })
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()

    expect(client.get).toHaveBeenCalledWith('/api/cases/7')
    expect(wrapper.text()).toContain('来源案例标题')
    expect(wrapper.text()).toContain('已审核通过的正式诊断依据')
    expect(wrapper.text()).not.toContain('error_logs')
  })

  it('待审核案例保留“非正式知识”状态提示', async () => {
    client.get.mockResolvedValue({ data: caseData(7, '待审案例', 'pending_review') })
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()
    expect(wrapper.text()).toContain('待审核(非正式知识)')
    expect(wrapper.text()).toContain('不能作为正式诊断依据')
  })

  it('非法 ID 不发请求并显示明确状态', async () => {
    routerState.route.params.id = 'abc'
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()

    expect(client.get).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('来源地址无效')
    expect(wrapper.find('[data-testid="case-detail-error"]').exists()).toBe(true)
  })

  it('403 显示无权状态,且不残留上一个案例正文', async () => {
    client.get.mockImplementation((url: string) =>
      url.endsWith('/8')
        ? Promise.reject(axiosError(403))
        : Promise.resolve({ data: caseData(7, '上一个案例正文') }),
    )
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()
    expect(wrapper.text()).toContain('上一个案例正文')

    routerState.route.params.id = '8'
    await flushPromises()

    expect(wrapper.text()).toContain('无权查看该案例')
    expect(wrapper.text()).not.toContain('上一个案例正文')
  })

  it('404 显示不存在状态', async () => {
    client.get.mockRejectedValue(axiosError(404))
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()
    expect(wrapper.text()).toContain('案例不存在或已被删除')
  })

  it('网络错误显示网络状态', async () => {
    client.get.mockRejectedValue(axiosError(undefined, 'Network Error'))
    const wrapper = mount(CaseDetailView, { global: globalCfg })
    await flushPromises()
    expect(wrapper.text()).toContain('网络错误')
  })

  it('快速切换 A/B:迟到的 A 响应不覆盖 B', async () => {
    const a = deferred<{ data: unknown }>()
    const b = deferred<{ data: unknown }>()
    client.get.mockImplementation((url: string) => (url.endsWith('/7') ? a.promise : b.promise))

    const wrapper = mount(CaseDetailView, { global: globalCfg })
    routerState.route.params.id = '8'
    await flushPromises()

    b.resolve({ data: caseData(8, '案例 B') })
    await flushPromises()
    expect(wrapper.text()).toContain('案例 B')

    a.resolve({ data: caseData(7, '案例 A') })
    await flushPromises()
    expect(wrapper.text()).toContain('案例 B')
    expect(wrapper.text()).not.toContain('案例 A')
  })
})

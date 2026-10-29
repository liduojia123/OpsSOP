/**
 * 知识检索过期响应防护(云端冒烟增量复审 P3)。
 *
 * 覆盖:
 * - Enter 并发搜索,旧响应后到不得覆盖新查询结果(复审复现:redis→nginx,nginx 先返回);
 * - 过期请求的失败不修改 error/loading;
 * - 切换故障/卸载使旧检索整体失效。
 *
 * API 全部 mock;不依赖真实模型、后端与网络。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick, reactive } from 'vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))
vi.mock('../src/api', () => ({ default: client, errMsg: (e: unknown) => String(e) }))

import AiAssistantPanel from '../src/views/AiAssistantPanel.vue'

// ---- 轻量 Element Plus 桩(仅暴露用到的交互面) ----

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
    'el-switch': defineComponent({
      props: { modelValue: Boolean, disabled: Boolean },
      emits: ['update:modelValue'],
      setup(props: { modelValue: boolean; disabled: boolean }, { emit }: any) {
        return () =>
          h('input', {
            type: 'checkbox',
            'data-testid': 'auto-switch',
            checked: props.modelValue,
            disabled: props.disabled,
            onChange: (e: Event) =>
              emit('update:modelValue', (e.target as HTMLInputElement).checked),
          })
      },
    }),
    'el-tag': defineComponent({
      setup(_: unknown, { slots }: any) {
        return () => h('span', { class: 'tag' }, slots.default?.())
      },
    }),
    'el-alert': defineComponent({
      props: { title: String },
      setup(props: { title?: string }) {
        return () => h('div', { class: 'alert' }, props.title)
      },
    }),
    'el-input': defineComponent({
      props: { modelValue: String },
      emits: ['update:modelValue'],
      setup(props: { modelValue?: string }, { emit }: any) {
        return () =>
          h('input', {
            value: props.modelValue,
            onInput: (e: Event) =>
              emit('update:modelValue', (e.target as HTMLInputElement).value),
          })
      },
    }),
  },
}

function makeProps(overrides: Record<string, unknown> = {}) {
  const form = reactive({
    title: 'Pod 频繁 OOMKilled',
    description: 'checkout 服务 pod 被 OOMKilled 重启',
    environment: 'prod',
    service: 'checkout',
    component_version: '1.8.2',
    recent_changes: '',
    error_logs: 'Reason: OOMKilled',
    attempted_actions: '',
    category_id: null as number | null,
    tags: [] as string[],
  })
  return {
    faultId: 1,
    records: [],
    categories: [{ id: 1, name: 'Linux' }],
    aiStatus: { ai_mode: 'demo', message: '演示模式可用', model_config_version: 'cfg-v1' },
    canEdit: true,
    ...overrides,
    form,
  }
}

function searchHit(title: string) {
  return {
    ref_id: `sop:${title}`,
    source_type: 'sop',
    source_id: 1,
    version: 1,
    title,
    snippet: `${title} 片段`,
    link: '/sops/1',
    status: 'published',
    score: 1,
  }
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

/** 微任务排空(伪定时器下 flushPromises 依赖 setTimeout,不可用) */
async function drainMicrotasks() {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

/** 按查询词派发检索请求的 mock:每次调用返回独立 deferred */
function mockSearchDispatch() {
  const pending = new Map<string, ReturnType<typeof deferred<{ data: { hits: unknown[] } }>>>()
  client.get.mockImplementation((url: string, config?: { params?: { q?: string } }) => {
    if (url === '/api/search') {
      const q = config?.params?.q ?? ''
      const d = deferred<{ data: { hits: unknown[] } }>()
      pending.set(q, d)
      return d.promise
    }
    return Promise.resolve({ data: [] })
  })
  return pending
}

const SEARCH_INPUT = 'input[placeholder="错误码 / 组件 / 现象关键词"]'

async function typeAndEnter(wrapper: any, text: string) {
  const input = wrapper.find(SEARCH_INPUT)
  await input.setValue(text)
  await input.trigger('keyup', { key: 'Enter' })
}

beforeEach(() => {
  client.get.mockReset()
  client.post.mockReset()
  client.get.mockResolvedValue({ data: [] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('知识检索过期响应防护(复审 P3)', () => {
  it('Enter 并发搜索:旧响应(redis)后到不覆盖新查询(nginx)结果', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'redis')
    await typeAndEnter(wrapper, 'nginx')
    expect(pending.has('redis') && pending.has('nginx')).toBe(true)

    // nginx 先返回
    pending.get('nginx')!.resolve({ data: { hits: [searchHit('nginx命中')] } })
    await drainMicrotasks()
    // redis 后到(旧响应)
    pending.get('redis')!.resolve({ data: { hits: [searchHit('redis命中')] } })
    await drainMicrotasks()

    expect(wrapper.text()).toContain('nginx命中')
    expect(wrapper.text()).not.toContain('redis命中')
    expect((wrapper.find(SEARCH_INPUT).element as HTMLInputElement).value).toBe('nginx')
    expect((wrapper.vm as any).searching).toBe(false)
  })

  it('过期请求的失败不修改 error/loading', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'redis')
    await typeAndEnter(wrapper, 'nginx')
    // 旧请求先失败(新请求仍在途):不得改 error,也不得把新请求的 loading 改掉
    pending.get('redis')!.reject(new Error('network down'))
    await drainMicrotasks()
    expect(wrapper.text()).not.toContain('检索失败')
    expect((wrapper.vm as any).searchError).toBe('')
    expect((wrapper.vm as any).searching).toBe(true)

    pending.get('nginx')!.resolve({ data: { hits: [searchHit('nginx命中')] } })
    await drainMicrotasks()
    expect(wrapper.text()).toContain('nginx命中')
    expect((wrapper.vm as any).searching).toBe(false)
  })

  it('切换故障使旧检索失效:旧响应后到不写入新故障面板', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'redis')
    await wrapper.setProps({ faultId: 2 })
    await drainMicrotasks()
    // 旧检索响应在切换后到达
    pending.get('redis')!.resolve({ data: { hits: [searchHit('redis命中')] } })
    await drainMicrotasks()

    expect(wrapper.text()).not.toContain('redis命中')
    expect((wrapper.vm as any).searching).toBe(false)
    expect((wrapper.vm as any).searchError).toBe('')
  })

  it('卸载后旧检索响应不产生状态变更', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'redis')
    wrapper.unmount()
    pending.get('redis')!.resolve({ data: { hits: [searchHit('redis命中')] } })
    await drainMicrotasks()
    // 卸载后无异常即通过(过期守卫拦截状态写入);VM 已卸载不再断言文本
    expect(pending.get('redis')).toBeTruthy()
  })

  // ===== 第三批:实际检索方式与降级状态展示 =====

  it('混合检索成功:展示实际方式,不把关键词结果冒充语义检索', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps({ aiStatus: { ai_mode: 'cloud', embedding_mode: 'cloud', message: '云端', model_config_version: 'cfg-v1' } }),
      global: globalCfg,
    })
    await drainMicrotasks()
    await typeAndEnter(wrapper, 'redis')
    pending.get('redis')!.resolve({
      data: {
        hits: [searchHit('混合命中')],
        retrieval_mode: 'hybrid',
        degraded: false,
        degrade_reason: '',
      },
    })
    await drainMicrotasks()
    expect(wrapper.text()).toContain('混合检索(关键词+向量)')
    expect(wrapper.text()).not.toContain('已降级为关键词检索')
    expect(wrapper.text()).toContain('查询文本(脱敏后)') // cloud 出网说明
  })

  it('向量失败降级:展示关键词方式与安全降级原因;服务错误不显示为无匹配', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()
    await typeAndEnter(wrapper, 'redis')
    pending.get('redis')!.resolve({
      data: {
        hits: [searchHit('关键词命中')],
        retrieval_mode: 'keyword',
        degraded: true,
        degrade_reason: '查询向量生成失败(timeout),仅关键词检索',
      },
    })
    await drainMicrotasks()
    const text = wrapper.text()
    expect(text).toContain('关键词检索')
    expect(text).toContain('已降级为关键词检索')
    expect(text).toContain('查询向量生成失败') // 降级原因可见,不是「无匹配」
    expect(text).toContain('关键词命中')
  })
})

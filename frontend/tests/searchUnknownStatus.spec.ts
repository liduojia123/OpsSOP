/**
 * AI 状态未知时的检索出网保护(第三批修复复审 P2-4)。
 *
 * 覆盖:
 * - 状态未知(aiStatus=null):检索走服务端纯关键词路径(keyword_only=true),
 *   零 embedding 外发,页面如实提示;关键词基本能力不依赖模型可达;
 * - 状态确认(如 cloud):正常混合检索,出网说明与实际数据流一致;
 * - 新检索失败后旧检索方式标签/旧降级横幅不残留(复审次要项)。
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
    aiStatus: { ai_mode: 'cloud', embedding_mode: 'cloud', message: '云端', model_config_version: 'cfg-v1' },
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

async function drainMicrotasks() {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

function mockSearchDispatch() {
  const pending = new Map<string, ReturnType<typeof deferred<{ data: unknown }>>>()
  client.get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url === '/api/search') {
      const q = String(config?.params?.q ?? '')
      const d = deferred<{ data: unknown }>()
      pending.set(q + '|' + JSON.stringify(config?.params ?? {}), d)
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

describe('AI 状态未知时的检索出网保护(复审 P2-4)', () => {
  it('状态未知:检索走 keyword_only 纯关键词路径并如实提示,零 embedding 外发', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps({ aiStatus: null }),
      global: globalCfg,
    })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'OOMKilled')
    await drainMicrotasks()
    // 请求必须带 keyword_only:服务端纯关键词路径,不生成查询向量
    const key = [...pending.keys()].find((k) => k.includes('OOMKilled'))
    expect(key).toBeTruthy()
    expect(key).toContain('keyword_only')
    expect(key).toContain('"keyword_only":true')

    pending.get(key!)!.resolve({
      data: {
        hits: [searchHit('关键词命中')],
        retrieval_mode: 'keyword',
        degraded: false,
        degrade_reason: '',
      },
    })
    await drainMicrotasks()
    const text = wrapper.text()
    expect(text).toContain('关键词命中') // 关键词基本能力保持
    expect(text).toContain('本次仅关键词检索') // 如实说明本次路径
    expect(text).toContain('查询文本未发送') // 零外发承诺与数据流一致
    expect(text).not.toContain('混合检索(关键词+向量)')
  })

  it('状态确认为 cloud:正常混合检索且出网说明与数据流一致', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps({
        aiStatus: { ai_mode: 'cloud', embedding_mode: 'cloud', message: '云端', model_config_version: 'cfg-v1' },
      }),
      global: globalCfg,
    })
    await drainMicrotasks()

    await typeAndEnter(wrapper, 'OOMKilled')
    await drainMicrotasks()
    const key = [...pending.keys()].find((k) => k.includes('OOMKilled'))
    expect(key).toBeTruthy()
    expect(key).not.toContain('keyword_only') // 状态已确认:允许混合路径

    pending.get(key!)!.resolve({
      data: {
        hits: [searchHit('混合命中')],
        retrieval_mode: 'hybrid',
        degraded: false,
        degrade_reason: '',
      },
    })
    await drainMicrotasks()
    const text = wrapper.text()
    expect(text).toContain('混合检索(关键词+向量)')
    expect(text).toContain('查询文本(脱敏后)') // cloud 出网说明可见
  })

  it('新检索失败后旧检索方式标签/旧降级横幅不残留', async () => {
    const pending = mockSearchDispatch()
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()

    // 第一次检索:降级成功,展示方式标签与降级横幅
    await typeAndEnter(wrapper, 'redis')
    const k1 = [...pending.keys()].find((k) => k.includes('redis'))!
    pending.get(k1)!.resolve({
      data: {
        hits: [searchHit('旧命中')],
        retrieval_mode: 'keyword',
        degraded: true,
        degrade_reason: '查询向量生成失败(timeout),仅关键词检索',
      },
    })
    await drainMicrotasks()
    expect(wrapper.text()).toContain('已降级为关键词检索')

    // 第二次检索:失败 → 旧标签/旧横幅不再代表本次结果
    await typeAndEnter(wrapper, 'nginx')
    const k2 = [...pending.keys()].find((k) => k.includes('nginx'))!
    pending.get(k2)!.reject(new Error('network down'))
    await drainMicrotasks()
    expect(wrapper.text()).toContain('检索失败')
    expect(wrapper.text()).not.toContain('已降级为关键词检索')
  })
})

afterEach(() => {
  vi.useRealTimers()
})

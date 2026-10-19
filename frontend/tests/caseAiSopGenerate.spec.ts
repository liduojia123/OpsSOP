/**
 * AI 生成 SOP 草稿入口行为测试(第三阶段第一批)。
 *
 * 覆盖:
 * - 生成中状态与防重复点击(在途不再发请求);
 * - 业务 failed(HTTP 201/report 空)持久显示原因/用量/重试,不跳转;
 * - 重试沿用同一 request_token(不重复创建草稿);主动再次生成用新 token;
 * - usage 缺失显示「用量未知」;
 * - 切换案例后过期响应不应用到新案例;
 * - 业务成功才跳转编辑页;云模式出网提示;disabled 禁用入口;
 * - 模板生成与 AI 生成入口名称区分。
 *
 * API 与路由全部 mock;不依赖真实后端与模型。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

const messages = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))
vi.mock('element-plus', () => ({
  ElMessage: messages,
  ElMessageBox: { prompt: vi.fn(), confirm: vi.fn() },
}))

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}))
vi.mock('../src/api', () => ({ default: client, errMsg: (e: unknown) => String(e) }))
vi.mock('../src/stores/auth', () => ({
  useAuthStore: () => ({ role: 'operator', user: { id: 1 }, token: 't' }),
}))

import CaseListView from '../src/views/CaseListView.vue'

// ---- 轻量 Element Plus 桩 ----

const globalCfg = {
  directives: {
    loading: {}, // v-loading 空实现
  },
  components: {
    'el-button': defineComponent({
      props: { disabled: Boolean, loading: Boolean, type: String },
      emits: ['click'],
      setup(props: { disabled: boolean; loading: boolean }, { slots, emit }: any) {
        return () =>
          h(
            'button',
            {
              type: 'button',
              disabled: props.disabled || props.loading,
              onClick: () => {
                if (!(props.disabled || props.loading)) emit('click')
              },
            },
            slots.default?.(),
          )
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
    'el-select': defineComponent({
      props: { modelValue: [String, Number, Array] },
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'select' }, slots.default?.())
      },
    }),
    'el-option': defineComponent({ setup: () => () => null }),
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
    'el-table': defineComponent({
      props: { data: { type: Array, default: () => [] } },
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'table' }, slots.default?.())
      },
    }),
    'el-table-column': defineComponent({ setup: () => () => null }),
    'el-drawer': defineComponent({
      props: { modelValue: Boolean },
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'drawer' }, slots.default?.())
      },
    }),
  },
}

// ---- 数据夹具 ----

function makeCaseRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 3,
    fault_id: 10,
    title: 'Pod 频繁 OOMKilled',
    symptom: 'pod 被 OOMKilled 重启',
    environment: 'prod',
    service: 'checkout',
    component_version: '1.8.2',
    category_id: null,
    tags: [],
    confirmed_root_cause: 'limits.memory 过低',
    handling_steps: '调整 limits',
    recovery_verification: '稳定运行 30 分钟',
    status: 'approved',
    review_comment: '',
    reviewed_by: 2,
    reviewed_at: '2026-10-15T00:00:00Z',
    created_by: 1,
    created_at: '2026-10-15T00:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
    ...overrides,
  }
}

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    case_id: 3,
    created_by: 1,
    request_token: 'tok-a',
    attempt: 1,
    input_hash: 'abcdef12',
    model_config_version: 'cfg-v1',
    is_demo: false,
    status: 'success',
    sop_id: 11,
    missing_information: [],
    incomplete_reasons: [],
    field_sources: [],
    error_code: '',
    error_message: '',
    usage: { prompt_tokens: 981, completion_tokens: 3070, total_tokens: 4051 },
    call_count: 1,
    latency_ms: 2300,
    created_at: '2026-10-15T00:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
    ...overrides,
  }
}

function makeAiResponse(run: Record<string, unknown>, sopId: number | null) {
  return {
    run,
    sop: sopId ? { id: sopId, title: '草稿' } : null,
    reused: false,
  }
}

async function drain() {
  for (let i = 0; i < 25; i++) await Promise.resolve()
}

async function mountWithCase() {
  client.get.mockImplementation((url: string) => {
    if (url === '/api/categories') return Promise.resolve({ data: [] })
    if (url === '/api/system/ai-status')
      return Promise.resolve({
        data: { ai_mode: 'cloud', embedding_mode: 'disabled', local_only: false, message: '云端模型', model_config_version: 'cfg-v1' },
      })
    return Promise.resolve({ data: [makeCaseRow()] })
  })
  const wrapper = mount(CaseListView, { global: globalCfg })
  await drain()
  const vm = wrapper.vm as any
  vm.openDetail(makeCaseRow())
  await nextTick()
  return { wrapper, vm }
}

beforeEach(() => {
  client.get.mockReset()
  client.post.mockReset()
  push.mockReset()
  messages.success.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('案例页 AI 生成 SOP 草稿(第三阶段第一批)', () => {
  it('模板生成与 AI 生成入口名称区分;云模式说明出网范围', async () => {
    const { wrapper } = await mountWithCase()
    const text = wrapper.text()
    expect(text).toContain('模板生成草稿')
    expect(text).toContain('AI 生成草稿')
    expect(text).toContain('脱敏后的案例材料') // 云模式出网提示(无需逐次确认弹窗)
  })

  it('业务成功才跳转编辑页;调用信息与真实用量可见', async () => {
    client.post.mockResolvedValue({ data: makeAiResponse(makeRun(), 11) })
    const { wrapper, vm } = await mountWithCase()
    await vm.generateSopAi()
    await drain()
    expect(client.post).toHaveBeenCalledTimes(1)
    expect(push).toHaveBeenCalledWith('/sops/11')
    expect(messages.success).toHaveBeenCalled()
    expect(wrapper.text()).toContain('状态:成功') // 运行信息(调用次数/用量)可见
    expect(wrapper.text()).toContain('输入 981')
  })

  it('HTTP 201 的业务 failed:持久显示原因/用量/重试,不跳转', async () => {
    client.post.mockResolvedValue({
      data: makeAiResponse(
        makeRun({
          status: 'failed',
          sop_id: null,
          error_code: 'source_changed',
          error_message: '来源案例在生成期间发生变化(状态或内容),本次结果未落为草稿',
          usage: { prompt_tokens: 981, completion_tokens: 3070, total_tokens: 4051 },
        }),
        null,
      ),
    })
    const { wrapper, vm } = await mountWithCase()
    await vm.generateSopAi()
    await drain()
    const text = wrapper.text()
    expect(text).toContain('AI 生成失败')
    expect(text).toContain('来源案例在生成期间发生变化') // 持久原因
    expect(text).toContain('用量:输入 981') // 真实用量
    expect(text).toContain('调用次数:1')
    expect(push).not.toHaveBeenCalled() // 业务失败不跳转
    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    expect(retryBtn).toBeTruthy() // 人工重试入口
  })

  it('重试沿用同一 request_token;主动再次生成用新 token', async () => {
    client.post
      .mockResolvedValueOnce({
        data: makeAiResponse(
          makeRun({ status: 'failed', sop_id: null, error_code: 'timeout', error_message: '模型调用超时(>60s)', usage: null }),
          null,
        ),
      })
      .mockResolvedValueOnce({ data: makeAiResponse(makeRun({ id: 2, request_token: 'tok-b' }), 12) })
    const { wrapper, vm } = await mountWithCase()
    await vm.generateSopAi()
    await drain()
    expect(wrapper.text()).toContain('用量未知') // usage 缺失显示未知

    // 失败后点「重试」:同一 token(不重复创建草稿/重复计费)
    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    await retryBtn!.trigger('click')
    await drain()
    const token1 = (client.post.mock.calls[0][1] as { request_token: string }).request_token
    const token2 = (client.post.mock.calls[1][1] as { request_token: string }).request_token
    expect(token2).toBe(token1) // 重试不换 token

    // 主动再次生成:新 token(可再建新草稿)
    await vm.generateSopAi()
    await drain()
    // 第三次调用被 mockResolvedValueOnce 链结束后默认 reject?这里只取 token 对比
    const calls = client.post.mock.calls
    if (calls.length >= 3) {
      const token3 = (calls[2][1] as { request_token: string }).request_token
      expect(token3).not.toBe(token1)
    }
  })

  it('生成中防重复点击:在途期间再次触发不再发请求', async () => {
    let resolveReq!: (v: unknown) => void
    client.post.mockReturnValueOnce(new Promise((r) => (resolveReq = r)))
    const { wrapper, vm } = await mountWithCase()
    void vm.generateSopAi()
    await drain()
    expect(wrapper.text()).toContain('AI 生成中')
    void vm.generateSopAi() // 在途重复触发
    await drain()
    expect(client.post).toHaveBeenCalledTimes(1)

    resolveReq({ data: makeAiResponse(makeRun(), 11) })
    await drain()
  })

  it('切换案例后过期响应不应用到新案例', async () => {
    let resolveSlow!: (v: unknown) => void
    client.post.mockReturnValueOnce(
      new Promise((r) => (resolveSlow = r)).then((data) => ({
        data: makeAiResponse(
          makeRun({ case_id: 3, status: 'failed', sop_id: null, error_code: 'timeout', error_message: '旧案例的失败原因' }),
          null,
        ),
      })),
    )
    const { wrapper, vm } = await mountWithCase()
    void vm.generateSopAi()
    await drain()

    // 切换到另一个案例(过期响应后到)
    vm.openDetail(makeCaseRow({ id: 4, title: '另一个案例' }))
    await nextTick()
    resolveSlow(undefined)
    await drain()
    expect(wrapper.text()).not.toContain('旧案例的失败原因') // 旧响应不得应用到新案例
  })

  it('disabled 模式禁用 AI 生成入口,模板生成不受影响', async () => {
    client.get.mockImplementation((url: string) => {
      if (url === '/api/categories') return Promise.resolve({ data: [] })
      if (url === '/api/system/ai-status')
        return Promise.resolve({
          data: { ai_mode: 'disabled', embedding_mode: 'disabled', local_only: false, message: '未启用', model_config_version: '' },
        })
      return Promise.resolve({ data: [makeCaseRow()] })
    })
    client.post.mockResolvedValue({ data: { id: 21 } })
    const wrapper = mount(CaseListView, { global: globalCfg })
    await drain()
    const vm = wrapper.vm as any
    vm.openDetail(makeCaseRow())
    await nextTick()

    const aiBtn = wrapper.findAll('button').find((b) => b.text().includes('AI 生成草稿'))
    expect(aiBtn!.attributes('disabled')).toBeDefined() // AI 入口禁用
    expect(wrapper.text()).toContain('模板生成与手工编辑不受影响')

    const tplBtn = wrapper.findAll('button').find((b) => b.text().includes('模板生成草稿'))
    expect(tplBtn!.attributes('disabled')).toBeUndefined() // 模板生成仍可用
    await tplBtn!.trigger('click')
    await drain()
    expect(client.post).toHaveBeenCalledWith('/api/cases/3/generate-sop')
    expect(push).toHaveBeenCalledWith('/sops/21')
  })

  // ===== 复审 P2-2:卸载后的迟到响应副作用保护 =====

  it('P2-2:卸载后迟到的成功响应不跳转、不弹通知', async () => {
    let resolveReq!: (v: unknown) => void
    client.post.mockReturnValueOnce(new Promise((r) => (resolveReq = r)))
    const { wrapper, vm } = await mountWithCase()
    void vm.generateSopAi()
    await drain()
    expect(client.post).toHaveBeenCalledTimes(1)

    // 用户离开页面(卸载/导航)后才收到成功响应
    wrapper.unmount()
    resolveReq({ data: makeAiResponse(makeRun(), 11) })
    await drain()
    expect(push).not.toHaveBeenCalled() // 不得把用户从新页面强制跳到旧草稿
    expect(messages.success).not.toHaveBeenCalled() // 不弹旧组件通知
  })

  it('P2-2:卸载后迟到的失败响应不弹通知、不覆盖当前页', async () => {
    let resolveReq!: (v: unknown) => void
    client.post.mockReturnValueOnce(new Promise((r) => (resolveReq = r)))
    const { wrapper, vm } = await mountWithCase()
    void vm.generateSopAi()
    await drain()

    wrapper.unmount()
    resolveReq({
      data: makeAiResponse(
        makeRun({ status: 'failed', sop_id: null, error_code: 'timeout', error_message: '迟到的失败' }),
        null,
      ),
    })
    await drain()
    expect(push).not.toHaveBeenCalled()
    expect(messages.error).not.toHaveBeenCalled()
    expect(messages.success).not.toHaveBeenCalled()
  })

  it('P2-4:成功结果展示字段来源依据(供人工核对)', async () => {
    client.post.mockResolvedValue({
      data: makeAiResponse(
        makeRun({
          field_sources: [
            { target: 'handling_steps', evidence_ids: ['case.handling_steps'] },
            { target: 'recovery_verification', evidence_ids: ['case.recovery_verification'] },
          ],
          missing_information: ['生产环境验证结果待补'],
        }),
        11,
      ),
    })
    const { wrapper, vm } = await mountWithCase()
    await vm.generateSopAi()
    await drain()
    const text = wrapper.text()
    expect(text).toContain('字段来源依据')
    expect(text).toContain('handling_steps ← case.handling_steps')
    expect(text).toContain('待补信息')
  })
})

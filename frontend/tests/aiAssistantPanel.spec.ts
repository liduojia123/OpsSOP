/**
 * AI 排查助手面板行为测试(第二阶段审查修复回归)。
 *
 * 覆盖审查缺陷:
 * - P1 #5 首次初始化:immediate watcher 不得访问未初始化 ref;进入即恢复分析历史;
 * - P2 #7 在途分析期间的补证:旧请求结束后重新排队分析最新输入,不丢弃最后一次变更;
 * - P2 #8 关闭自动分析:清除已排定的防抖计时器,发送前复检开关;
 * - 历史时序:切换故障后旧历史响应不覆盖新故障历史;
 * - P2-2(2026-10-15)业务 failed 展示:HTTP 201 的 failed/report=null 与历史 failed
 *   统一显示失败原因/用量/重试;元信息独立于 report;旧成功报告保留时标注旧结果。
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

// ---- 数据夹具 ----

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

function analysisRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    fault_id: 1,
    created_by: 1,
    trigger_type: 'manual',
    input_hash: 'abcdef12',
    model_config_version: 'cfg-v1',
    knowledge_version: 'k1',
    is_demo: true,
    status: 'success',
    report: {
      summary: '摘要',
      observed_facts: [],
      suggested_category: null,
      suggested_tags: [],
      hypotheses: [],
      missing_information: [],
      next_checks: [],
      references: [],
      insufficient_evidence: false,
    },
    incomplete_reasons: [],
    error_code: '',
    error_message: '',
    usage: null,
    call_count: 0,
    latency_ms: 0,
    created_at: '2026-10-14T00:00:00Z',
    tool_calls: [],
    ...overrides,
  }
}

function analyzeResponse(overrides: Record<string, unknown> = {}) {
  return {
    analysis: analysisRow(overrides),
    reused: false,
    input_substance: true,
  }
}

/** 微任务排空(伪定时器下 flushPromises 依赖 setTimeout,不可用) */
async function drainMicrotasks() {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

beforeEach(() => {
  client.get.mockReset()
  client.post.mockReset()
  client.get.mockResolvedValue({ data: [] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('AiAssistantPanel(第二阶段审查修复回归)', () => {
  it('P1 #5:首次进入初始化无引用错误,并恢复已有分析历史', async () => {
    client.get.mockResolvedValueOnce({ data: [analysisRow({ id: 7 })] })
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps(),
      global: globalCfg,
    })
    await drainMicrotasks()
    // 修复前:immediate watcher 在 TDZ 处中断,loadHistory 不会被调用
    expect(client.get).toHaveBeenCalledWith(
      '/api/faults/1/analyses',
      expect.objectContaining({ params: { limit: 20 } }),
    )
    // 历史恢复到面板(倒序列表存在)
    expect(wrapper.text()).toContain('分析历史(1)')
  })

  it('查看历史报告:标记为非实时结果(过期提示)', async () => {
    client.get.mockResolvedValueOnce({ data: [analysisRow({ id: 7 })] })
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps(),
      global: globalCfg,
    })
    await drainMicrotasks()
    const vm = wrapper.vm as any
    vm.historyOpen = true
    await nextTick()
    vm.viewHistoryItem(vm.history[0])
    await nextTick()
    expect(vm.stale).toBe(true)
    expect(wrapper.text()).toContain('历史报告')
  })

  it('P2 #8:关闭自动分析后,输入停顿不再发请求', async () => {
    vi.useFakeTimers()
    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()
    // 关闭自动开关
    await wrapper.find('input[data-testid="auto-switch"]').setValue(false)
    props.form.description = '关闭后的新输入内容'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2500)
    await drainMicrotasks()
    expect(client.post).not.toHaveBeenCalled()
  })

  it('P2 #8:输入后在防抖窗口内关闭开关,已排定的自动分析被清除', async () => {
    vi.useFakeTimers()
    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()
    // 输入 → 排定 2 秒防抖
    props.form.description = '触发防抖的输入'
    await nextTick()
    // 窗口内关闭开关 → 清除计时器
    await wrapper.find('input[data-testid="auto-switch"]').setValue(false)
    await vi.advanceTimersByTimeAsync(2500)
    await drainMicrotasks()
    expect(client.post).not.toHaveBeenCalled()
  })

  it('P2 #7:在途分析期间补充证据,旧请求结束后重新分析最新输入', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (v: unknown) => void
    const firstCall = new Promise((r) => {
      resolveFirst = r
    })
    client.post
      .mockReturnValueOnce(firstCall)
      .mockResolvedValue({ data: analyzeResponse({ id: 2 }) })

    const props = makeProps()
    mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()

    // 1) 第一次自动分析发出(进入在途)
    props.form.description = '第一版输入'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1)

    // 2) 在途期间补证:停顿两秒的自动分析不得被丢弃
    props.form.description = '第二版输入(补证)'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1) // 仍在途,记录待排队

    // 3) 旧请求结束 → 自动重新排队分析最新输入
    resolveFirst({ data: analyzeResponse({ id: 1 }) })
    await drainMicrotasks()
    await vi.advanceTimersByTimeAsync(400)
    await drainMicrotasks()

    expect(client.post).toHaveBeenCalledTimes(2)
    const secondBody = client.post.mock.calls[1][1]
    expect(secondBody.input.description).toContain('第二版输入')
  })

  it('切换故障后,旧历史响应不覆盖新故障历史', async () => {
    let resolveSlow!: (v: unknown) => void
    const slow = new Promise((r) => {
      resolveSlow = r
    })
    client.get
      .mockReturnValueOnce(slow) // 故障 1 历史(慢)
      .mockResolvedValueOnce({ data: [analysisRow({ id: 22, fault_id: 2 })] }) // 故障 2 历史(快)

    const wrapper = mount(AiAssistantPanel, {
      props: makeProps(),
      global: globalCfg,
    })
    await wrapper.setProps({ faultId: 2 })
    await drainMicrotasks()
    // 故障 1 的旧响应后到,必须被丢弃
    resolveSlow({ data: [analysisRow({ id: 11, fault_id: 1 })] })
    await drainMicrotasks()

    const hist = (wrapper.vm as any).history as Array<{ id: number }>
    expect(hist.map((h) => h.id)).toEqual([22])
  })

  it('P2 #3:卸载后不再补发自动分析(待排队任务整体失效)', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (v: unknown) => void
    const firstCall = new Promise((r) => {
      resolveFirst = r
    })
    client.post.mockReturnValueOnce(firstCall).mockResolvedValue({ data: analyzeResponse({ id: 2 }) })

    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()

    // 1) 自动分析发出(进入在途)
    props.form.description = '第一版输入'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1)

    // 2) 在途期间补充证据 → 记入待排队
    props.form.description = '离开前补充的证据'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1)

    // 3) 离开页面(卸载)后旧请求才结束:不得再补发请求
    wrapper.unmount()
    resolveFirst({ data: analyzeResponse({ id: 1 }) })
    await drainMicrotasks()
    await vi.advanceTimersByTimeAsync(1000) // 覆盖 300ms 补发窗口
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1) // 修复前:卸载后会再 POST 同一故障
  })

  it('切换故障后 analyzing 复位,新故障可立即自动分析', async () => {
    vi.useFakeTimers()
    let resolveFirst!: (v: unknown) => void
    const firstCall = new Promise((r) => {
      resolveFirst = r
    })
    client.post.mockReturnValueOnce(firstCall).mockResolvedValue({ data: analyzeResponse({ id: 2 }) })

    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()

    props.form.description = '故障 1 的输入'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(1)

    // 在途切换故障:旧请求作废,analyzing 必须复位
    await wrapper.setProps({ faultId: 2 })
    await drainMicrotasks()
    expect((wrapper.vm as any).analyzing).toBe(false)

    props.form.description = '故障 2 的输入'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2000)
    await drainMicrotasks()
    expect(client.post).toHaveBeenCalledTimes(2) // 修复前:被当成在途而丢弃
    expect(client.post.mock.calls[1][0]).toBe('/api/faults/2/analyze')

    resolveFirst({ data: analyzeResponse({ id: 1 }) }) // 收尾旧请求
    await drainMicrotasks()
  })

  it('P2 #4:AI 状态未知时不自动调用,并提供重试入口', async () => {
    vi.useFakeTimers()
    const props = makeProps({ aiStatus: null })
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()

    // 状态未知提示与重试按钮
    expect(wrapper.text()).toContain('AI 状态未知')
    props.form.description = '状态未知时的输入'
    await nextTick()
    await vi.advanceTimersByTimeAsync(2500)
    await drainMicrotasks()
    expect(client.post).not.toHaveBeenCalled() // 未确认模式/出网提示前不发送

    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('重新加载状态'))
    expect(retryBtn).toBeTruthy()
    await retryBtn!.trigger('click')
    expect(wrapper.emitted('retry-status')).toBeTruthy()
  })

  it('P2 #6:部分字段缺失的 usage 按字段显示未知,不虚构合计', async () => {
    client.get.mockResolvedValueOnce({
      data: [analysisRow({ id: 7, usage: { prompt_tokens: 15 }, call_count: 2 })],
    })
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps(),
      global: globalCfg,
    })
    await drainMicrotasks()
    const vm = wrapper.vm as any
    vm.viewHistoryItem(vm.history[0])
    await nextTick()
    expect(wrapper.text()).toContain('输入 15')
    expect(wrapper.text()).toContain('未知')
    expect(wrapper.text()).not.toContain('合计 15') // 缺失字段不得沿用单次值当合计
  })

  // ===== 第二阶段真实联调复审回归(2026-10-15 复审 P2-2)=====

  it('P2-2:HTTP 201 的 failed(report=null)持久显示原因/用量/重试,不空白', async () => {
    client.post.mockResolvedValue({
      data: analyzeResponse({
        id: 7,
        is_demo: false,
        status: 'failed',
        report: null,
        error_code: 'schema_mismatch',
        error_message: '模型输出不符合 Schema: references.0.version',
        usage: { prompt_tokens: 981, completion_tokens: 3070, total_tokens: 4051 },
        call_count: 1,
        latency_ms: 2300,
      }),
    })
    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()
    await (wrapper.vm as any).analyze('manual')
    await drainMicrotasks()

    const text = wrapper.text()
    // 持久失败原因(HTTP 201 不是成功判据;业务 failed 要有展示分支)
    expect(text).toContain('分析失败')
    expect(text).toContain('模型输出不符合 Schema')
    // 真实用量与调用次数可见(独立于 report)
    expect(text).toContain('用量:输入 981')
    expect(text).toContain('调用次数:1')
    // 人工重试入口
    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    expect(retryBtn).toBeTruthy()
    // 输入保留;不以空白代替失败状态
    expect(props.form.description).toContain('checkout')
    expect(text).not.toContain('尚无分析结果')
  })

  it('P2-2:点击历史 failed 显示失败原因与用量(不只提示历史报告)', async () => {
    client.get.mockResolvedValueOnce({
      data: [
        analysisRow({
          id: 8,
          status: 'failed',
          report: null,
          error_code: 'timeout',
          error_message: '模型调用超时(>60s)',
          usage: null,
          call_count: 2,
        }),
      ],
    })
    const wrapper = mount(AiAssistantPanel, {
      props: makeProps(),
      global: globalCfg,
    })
    await drainMicrotasks()
    const vm = wrapper.vm as any
    vm.viewHistoryItem(vm.history[0])
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('模型调用超时') // 历史 failed 的 error_message 持久显示
    expect(text).toContain('用量未知') // usage 缺失显示未知
    expect(text).toContain('调用次数:2')
    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    expect(retryBtn).toBeTruthy() // 历史 failed 同样有人工重试入口
  })

  it('P2-2:请求失败保留上一份成功报告并标注旧结果', async () => {
    client.post
      .mockResolvedValueOnce({
        data: analyzeResponse({
          id: 3,
          report: {
            summary: '上一份成功摘要',
            observed_facts: [],
            suggested_category: null,
            suggested_tags: [],
            hypotheses: [],
            missing_information: [],
            next_checks: [],
            references: [],
            insufficient_evidence: false,
          },
        }),
      })
      .mockRejectedValueOnce(new Error('network down'))

    const props = makeProps()
    const wrapper = mount(AiAssistantPanel, { props, global: globalCfg })
    await drainMicrotasks()
    const vm = wrapper.vm as any

    await vm.analyze('manual')
    await drainMicrotasks()
    expect(wrapper.text()).toContain('上一份成功摘要')

    await vm.analyze('manual')
    await drainMicrotasks()
    const text = wrapper.text()
    expect(text).toContain('上一份成功摘要') // 旧成功报告保留
    expect(text).toContain('旧结果') // 必须明确标注为旧结果,不冒充本次结果
    expect(text).toContain('分析失败') // 失败原因可见
  })
})

describe('AiAssistantPanel 实时查询展示', () => {
  it('展示 AI 工具调用与“未使用缓存”标注', async () => {
    client.post.mockResolvedValueOnce({
      data: analyzeResponse({
        tool_calls: [
          {
            id: 1,
            tool: 'prometheus_query',
            arguments: { query: 'node_load1' },
            status: 'ok',
            result_excerpt: 'last=1.9 min=0 max=2.87',
            error: '',
            latency_ms: 20,
            created_at: '2026-10-29T00:00:00Z',
          },
          {
            id: 2,
            tool: 'ssh_run',
            arguments: { server: 'ubuntu1', template: 'uptime' },
            status: 'ok',
            result_excerpt: 'up 1 day',
            error: '',
            latency_ms: 210,
            created_at: '2026-10-29T00:00:00Z',
          },
        ],
      }),
    })
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await drainMicrotasks()
    await (wrapper.vm as any).analyze('manual')
    await drainMicrotasks()

    expect(wrapper.text()).toContain('AI 实时查询')
    expect(wrapper.text()).toContain('Prometheus 查询')
    expect(wrapper.text()).toContain('SSH 只读命令')
    expect(wrapper.text()).toContain('实时查询 · 未使用缓存')
    expect(wrapper.text()).toContain('node_load1')
    expect(wrapper.text()).toContain('ubuntu1 · uptime')
  })
})

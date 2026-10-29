/**
 * 右侧助手的来源定位入口(第四阶段第一批)。
 *
 * 覆盖:
 * - 分析报告引用的案例/SOP 均提供可操作入口;
 * - 搜索命中同样提供案例/SOP 入口;
 * - 地址只由受控 source_type/source_id 构造,非法来源不渲染链接;
 * - 链接使用新标签页,避免离开页面丢失左侧未保存输入。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, reactive } from 'vue'

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}))

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
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

function makeProps() {
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
    form,
  }
}

function analysisRow() {
  return {
    id: 1,
    fault_id: 1,
    created_by: 1,
    trigger_type: 'manual',
    input_hash: 'hash-1',
    model_config_version: 'cfg-v1',
    knowledge_version: 'k-1',
    is_demo: true,
    status: 'success',
    report: {
      summary: '模拟摘要',
      observed_facts: [],
      suggested_category: null,
      suggested_tags: [],
      hypotheses: [],
      missing_information: [],
      next_checks: [],
      references: [
        {
          ref_id: 'case:7',
          source_type: 'case',
          source_id: 7,
          version: null,
          relevance: '案例依据',
        },
        {
          ref_id: 'sop:9',
          source_type: 'sop',
          source_id: 9,
          version: 2,
          relevance: 'SOP 依据',
        },
        {
          ref_id: 'evil:1',
          source_type: 'https://evil.example',
          source_id: 1,
          version: null,
          relevance: '非法来源',
        },
      ],
      insufficient_evidence: false,
    },
    incomplete_reasons: [],
    error_code: '',
    error_message: '',
    usage: null,
    call_count: 0,
    latency_ms: 0,
    created_at: '2026-10-21T00:00:00Z',
  }
}

const SEARCH_INPUT = 'input[placeholder="错误码 / 组件 / 现象关键词"]'

beforeEach(() => {
  client.get.mockReset()
  client.post.mockReset()
  client.get.mockImplementation((url: string) => {
    if (url === '/api/faults/1/analyses') return Promise.resolve({ data: [] })
    if (url === '/api/search') {
      return Promise.resolve({
        data: {
          hits: [
            {
              ref_id: 'case:11',
              source_type: 'case',
              source_id: 11,
              version: null,
              title: '搜索案例',
              snippet: '案例片段',
              link: '/cases/11',
              status: 'approved',
              score: 2,
            },
            {
              ref_id: 'sop:12',
              source_type: 'sop',
              source_id: 12,
              version: 3,
              title: '搜索 SOP',
              snippet: 'SOP 片段',
              link: '/sops/12',
              status: 'published',
              score: 1,
            },
          ],
          retrieval_mode: 'keyword',
          degraded: false,
          degrade_reason: '',
        },
      })
    }
    return Promise.resolve({ data: [] })
  })
  client.post.mockResolvedValue({
    data: { analysis: analysisRow(), reused: false, input_substance: true },
  })
})

describe('助手来源定位入口', () => {
  it('分析报告引用提供案例/SOP 新标签页入口,非法来源不渲染链接', async () => {
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await flushPromises()

    const buttons = wrapper.findAll('button')
    const analyzeBtn = buttons.find((b) => b.text().includes('立即分析'))
    expect(analyzeBtn).toBeTruthy()
    await analyzeBtn!.trigger('click')
    await flushPromises()

    const links = wrapper.findAll('a[data-testid="reference-source-link"]')
    expect(links).toHaveLength(2)
    const hrefs = links.map((l) => l.attributes('href'))
    expect(hrefs).toEqual(expect.arrayContaining(['/cases/7', '/sops/9']))
    for (const link of links) {
      expect(link.attributes('target')).toBe('_blank')
      expect(link.attributes('rel')).toContain('noopener')
    }
    expect(wrapper.html()).not.toContain('https://evil.example')
    expect(wrapper.text()).toContain('来源无效')
    expect(wrapper.text()).toContain('v2')
  })

  it('搜索命中的案例与 SOP 均可定位', async () => {
    const wrapper = mount(AiAssistantPanel, { props: makeProps(), global: globalCfg })
    await flushPromises()

    const input = wrapper.find(SEARCH_INPUT)
    await input.setValue('OOMKilled')
    await input.trigger('keyup', { key: 'Enter' })
    await flushPromises()

    const links = wrapper.findAll('a[data-testid="search-hit-source-link"]')
    expect(links).toHaveLength(2)
    const hrefs = links.map((l) => l.attributes('href'))
    expect(hrefs).toEqual(expect.arrayContaining(['/cases/11', '/sops/12']))
    expect(wrapper.text()).toContain('关键词检索')
    expect(wrapper.text()).toContain('不影响当前未保存输入')
  })
})

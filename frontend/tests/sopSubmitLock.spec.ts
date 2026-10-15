/**
 * SOP 提交审核期间锁定编辑(第二阶段审查 P2 #6 回归)。
 *
 * 修复前:submitForReview 的 PATCH/POST 在途时表单仍可编辑,提交后的 load()
 * 会用旧快照覆盖请求期间新输入。修复后:保存/提交期间 el-form 置 disabled,
 * 按钮互斥,请求完成后恢复。
 *
 * API 与路由全部 mock;不依赖真实后端。
 */
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}))
vi.mock('../src/api', () => ({ default: client, errMsg: (e: unknown) => String(e) }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '5' } }),
  useRouter: () => ({ push: vi.fn() }),
}))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))
vi.mock('../src/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', user: { id: 1 }, token: 't' }),
}))

import SopDetailView from '../src/views/SopDetailView.vue'

// ---- 轻量 Element Plus 桩:表单/按钮暴露 disabled 面 ----

const globalCfg = {
  components: {
    'el-form': defineComponent({
      props: { disabled: Boolean },
      setup(props: { disabled: boolean }, { slots }: any) {
        return () => h('form', { 'data-disabled': String(!!props.disabled) }, slots.default?.())
      },
    }),
    'el-form-item': defineComponent({
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'form-item' }, slots.default?.())
      },
    }),
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
    'el-input': defineComponent({
      props: { modelValue: String },
      emits: ['update:modelValue'],
      setup(props: { modelValue?: string }, { emit }: any) {
        return () =>
          h('textarea', {
            value: props.modelValue,
            onInput: (e: Event) =>
              emit('update:modelValue', (e.target as HTMLTextAreaElement).value),
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
    'el-dialog': defineComponent({
      props: { modelValue: Boolean },
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'dialog' }, slots.default?.())
      },
    }),
    'el-radio-group': defineComponent({
      props: { modelValue: String },
      setup(_: unknown, { slots }: any) {
        return () => h('div', { class: 'radio-group' }, slots.default?.())
      },
    }),
    'el-radio': defineComponent({
      setup(_: unknown, { slots }: any) {
        return () => h('span', { class: 'radio' }, slots.default?.())
      },
    }),
  },
}

const sopData = {
  id: 5,
  doc_id: 'doc-1',
  version: 1,
  title: '草稿标题',
  category_id: 1,
  tags: ['redis'],
  applicable_env: 'prod',
  symptom: '连接超时',
  preconditions: '',
  check_steps: '检查网络',
  handling_steps: '重启实例',
  recovery_verification: '连接恢复',
  rollback_steps: '',
  notes: '',
  source_case_id: 1,
  maintainer_id: null,
  status: 'draft',
  review_comment: '',
  reviewed_by: null,
  reviewed_at: null,
  created_by: 1,
  created_at: '2026-10-14T00:00:00Z',
  updated_at: '2026-10-14T00:00:00Z',
  published_at: null,
}

function findButton(wrapper: ReturnType<typeof mount>, text: string) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes(text))
  expect(btn, `按钮「${text}」应存在`).toBeTruthy()
  return btn!
}

describe('SopDetailView 提交锁定(第二阶段审查 P2 #6)', () => {
  it('提交审核在途时锁定表单与保存按钮,完成后恢复', async () => {
    client.get.mockImplementation((url: string) => {
      if (url === '/api/sops/5') return Promise.resolve({ data: { ...sopData } })
      return Promise.resolve({ data: [] })
    })
    let resolvePatch!: (v: unknown) => void
    const patchInFlight = new Promise((r) => {
      resolvePatch = r
    })
    client.patch.mockReturnValueOnce(patchInFlight)
    client.post.mockResolvedValue({ data: {} })

    const wrapper = mount(SopDetailView, { global: globalCfg })
    await flushPromises()
    await flushPromises()

    // 主编辑表单(第一个 form;弹窗内反馈表单在后)
    const mainForm = () => wrapper.findAll('form')[0]
    expect(mainForm().attributes('data-disabled')).toBe('false')

    // 点击「提交审核」→ PATCH 进入在途
    await findButton(wrapper, '提交审核').trigger('click')
    await wrapper.vm.$nextTick()

    // 修复前:此处表单仍可编辑,请求期间的新输入会被提交后的旧快照覆盖
    expect(mainForm().attributes('data-disabled')).toBe('true')
    expect((findButton(wrapper, '保存').element as HTMLButtonElement).disabled).toBe(true)
    expect(client.post).not.toHaveBeenCalled() // PATCH 未完成,不进入提交

    // PATCH 完成 → POST 提交 → load() 刷新
    resolvePatch({ data: { ...sopData, updated_at: '2026-10-14T00:00:01Z' } })
    await flushPromises()
    await flushPromises()
    await flushPromises()

    expect(client.post).toHaveBeenCalledWith('/api/sops/5/submit', { comment: '' })
    // 提交完成后恢复可编辑(表单已按服务端快照刷新)
    expect(mainForm().attributes('data-disabled')).toBe('false')
  })

  it('保存在途时提交按钮禁用,防并发冲突操作', async () => {
    client.get.mockImplementation((url: string) => {
      if (url === '/api/sops/5') return Promise.resolve({ data: { ...sopData } })
      return Promise.resolve({ data: [] })
    })
    let resolvePatch!: (v: unknown) => void
    const patchInFlight = new Promise((r) => {
      resolvePatch = r
    })
    client.patch.mockReturnValueOnce(patchInFlight)

    const wrapper = mount(SopDetailView, { global: globalCfg })
    await flushPromises()
    await flushPromises()

    await findButton(wrapper, '保存').trigger('click')
    await wrapper.vm.$nextTick()

    const submitBtn = findButton(wrapper, '提交审核')
    expect((submitBtn.element as HTMLButtonElement).disabled).toBe(true)

    resolvePatch({ data: { ...sopData, updated_at: '2026-10-14T00:00:02Z' } })
    await flushPromises()
    expect((findButton(wrapper, '提交审核').element as HTMLButtonElement).disabled).toBe(false)
  })
})

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const mocks = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
  auth: {
    token: 'session',
    user: {
      id: 1,
      username: 'op_user',
      display_name: '值班一号',
      signature: '先取证，再动手',
      avatar: 'data:image/png;base64,AAAA',
      role: 'operator',
      is_active: true,
      created_at: '2026-10-01T00:00:00Z',
    },
    logout: vi.fn(),
    updateUser: vi.fn(),
  },
  router: { replace: vi.fn() },
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))
vi.mock('../src/api', () => ({ default: mocks.api, errMsg: (e: Error) => e.message }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => mocks.auth }))
vi.mock('vue-router', () => ({ useRouter: () => mocks.router }))
vi.mock('element-plus', () => ({ ElMessage: mocks.message }))

import ProfileDialog from '../src/components/ProfileDialog.vue'

const stubs = {
  'el-dialog': defineComponent({
    props: { modelValue: Boolean, title: String },
    setup(_: unknown, { slots }: any) {
      return () => h('div', { class: 'dialog' }, [slots.default?.(), slots.footer?.()])
    },
  }),
  'el-button': defineComponent({
    props: { disabled: Boolean, loading: Boolean },
    emits: ['click'],
    setup(_: unknown, { slots, emit }: any) {
      return () => h('button', { onClick: () => emit('click') }, slots.default?.())
    },
  }),
  'el-input': defineComponent({
    props: { modelValue: { type: String, default: '' }, type: { type: String, default: 'text' } },
    emits: ['update:modelValue'],
    setup(props: { modelValue: string; type: string }, { emit }: any) {
      return () =>
        props.type === 'textarea'
          ? h('textarea', {
              value: props.modelValue,
              onInput: (event: Event) =>
                emit('update:modelValue', (event.target as HTMLTextAreaElement).value),
            })
          : h('input', {
              value: props.modelValue,
              onInput: (event: Event) =>
                emit('update:modelValue', (event.target as HTMLInputElement).value),
            })
    },
  }),
  'el-form': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('form', slots.default?.())
    },
  }),
  'el-form-item': defineComponent({
    setup(_: unknown, { slots }: any) {
      return () => h('div', slots.default?.())
    },
  }),
}

function mountDialog() {
  return mount(ProfileDialog, {
    props: { modelValue: true },
    global: { components: stubs },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.api.patch.mockResolvedValue({ data: { ...mocks.auth.user, signature: '新签名' } })
  mocks.api.post.mockResolvedValue({ data: null })
  mocks.router.replace.mockResolvedValue(undefined)
})

describe('ProfileDialog', () => {
  it('展示当前头像、签名与身份信息', () => {
    const wrapper = mountDialog()
    expect(wrapper.find('.profile-avatar img').attributes('src')).toBe(mocks.auth.user.avatar)
    expect(wrapper.find('textarea').element.value).toBe('先取证，再动手')
    expect(wrapper.text()).toContain('值班一号')
    expect(wrapper.text()).toContain('op_user')
  })

  it('保存资料时提交签名与头像并更新会话', async () => {
    const wrapper = mountDialog()
    await wrapper.find('textarea').setValue('  新的签名  ')
    const save = wrapper.findAll('button').find((b) => b.text().includes('保存资料'))
    expect(save).toBeTruthy()
    await save!.trigger('click')
    await flushPromises()

    expect(mocks.api.patch).toHaveBeenCalledWith('/api/auth/profile', {
      signature: '新的签名',
      avatar: mocks.auth.user.avatar,
    })
    expect(mocks.auth.updateUser).toHaveBeenCalledWith(
      expect.objectContaining({ signature: '新签名' }),
    )
    expect(mocks.message.success).toHaveBeenCalled()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('两次新密码不一致时提示且不发请求', async () => {
    const wrapper = mountDialog()
    const inputs = wrapper.find('.profile-password').findAll('input')
    await inputs[0].setValue('old-password')
    await inputs[1].setValue('new-password')
    await inputs[2].setValue('different')
    const button = wrapper.findAll('button').find((b) => b.text().includes('修改密码'))
    await button!.trigger('click')
    expect(mocks.message.warning).toHaveBeenCalledWith('两次输入的新密码不一致')
    expect(mocks.api.post).not.toHaveBeenCalled()
  })

  it('密码修改成功后退出并跳转登录页', async () => {
    const wrapper = mountDialog()
    const inputs = wrapper.find('.profile-password').findAll('input')
    await inputs[0].setValue('old-password')
    await inputs[1].setValue('new-password')
    await inputs[2].setValue('new-password')
    const button = wrapper.findAll('button').find((b) => b.text().includes('修改密码'))
    await button!.trigger('click')
    await flushPromises()

    expect(mocks.api.post).toHaveBeenCalledWith('/api/auth/change-password', {
      current_password: 'old-password',
      new_password: 'new-password',
    })
    expect(mocks.auth.logout).toHaveBeenCalledOnce()
    expect(mocks.router.replace).toHaveBeenCalledWith({ name: 'login' })
  })
})
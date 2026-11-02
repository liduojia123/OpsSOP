import { afterEach, expect, it, vi } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import FeedbackPreview from '../src/components/FeedbackPreview.vue'
import { requestActivity } from '../src/requestActivity'

const dialog = defineComponent({ template: '<div><slot /><slot name="footer" /></div>' })
const button = defineComponent({ template: '<button><slot /></button>' })
afterEach(() => vi.useRealTimers())

it('关闭演示或离开页面会释放加载状态,计时器不会留下假请求', async () => {
  vi.useFakeTimers()
  const wrapper = mount(FeedbackPreview, { props: { modelValue: true }, global: { stubs: { ElDialog: dialog, ElButton: button } } })
  await wrapper.findAll('button')[1].trigger('click')
  expect(requestActivity.pending).toBe(1)
  await wrapper.setProps({ modelValue: false })
  expect(requestActivity.pending).toBe(0)
  await vi.advanceTimersByTimeAsync(2500)
  expect(wrapper.text()).not.toContain('已完成 · 演示')
  await wrapper.setProps({ modelValue: true })
  await wrapper.findAll('button')[0].trigger('click')
  await vi.advanceTimersByTimeAsync(4500)
  expect(wrapper.text()).toContain('连接未完成 · 演示')
  expect(requestActivity.pending).toBe(0)
  await wrapper.findAll('button')[1].trigger('click')
  await nextTick()
  expect(requestActivity.pending).toBe(1)
  wrapper.unmount()
  await vi.advanceTimersByTimeAsync(2500)
  expect(requestActivity.pending).toBe(0)
})

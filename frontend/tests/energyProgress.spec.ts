import { expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import EnergyProgress from '../src/components/EnergyProgress.vue'

it('仅确定进度显示百分比,未知进度保持不定状态,完成与异常语义正确', async () => {
  const wrapper = mount(EnergyProgress, { props: { running: false } })
  expect(wrapper.attributes('aria-valuenow')).toBeUndefined()
  expect(wrapper.text()).not.toContain('%')
  await wrapper.setProps({ progress: 42 })
  expect(wrapper.attributes('aria-valuenow')).toBe('42')
  expect(wrapper.text()).toContain('42%')
  await wrapper.setProps({ progress: 180 })
  expect(wrapper.attributes('aria-valuenow')).toBe('100')
  await wrapper.setProps({ phase: 'error', progress: 66 })
  expect(wrapper.classes()).toContain('is-error')
  expect(wrapper.attributes('aria-valuenow')).toBe('66')
  wrapper.unmount()
})

it('减少动态不启动绘制循环,离开时取消动画和监听', () => {
  const remove = vi.fn()
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: remove }))
  const frame = vi.spyOn(window, 'requestAnimationFrame')
  const wrapper = mount(EnergyProgress)
  expect(frame).not.toHaveBeenCalled()
  wrapper.unmount()
  expect(remove).toHaveBeenCalled()
  frame.mockRestore()
  vi.unstubAllGlobals()
})

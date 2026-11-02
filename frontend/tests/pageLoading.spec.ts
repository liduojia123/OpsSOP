import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import PageLoading from '../src/components/PageLoading.vue'

it('等待状态说明当前任务，未知进度不显示百分比', () => {
  const wrapper = mount(PageLoading, { props: { label: '正在读取案例', compact: true } })
  expect(wrapper.get('[role="status"]').attributes('aria-label')).toBe('正在读取案例')
  expect(wrapper.text()).toContain('正在读取案例')
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
  expect(wrapper.text()).not.toContain('%')
  wrapper.unmount()
})

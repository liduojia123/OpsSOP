import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { AxiosError, CanceledError } from 'axios'

vi.mock('../src/stores/auth', () => ({ useAuthStore: () => ({ token: '', logout: vi.fn() }) }))
vi.mock('../src/router', () => ({ default: { currentRoute: { value: { name: 'login' } }, push: vi.fn() } }))
import client from '../src/api'
import { requestActivity, startRequest, endRequest } from '../src/requestActivity'
import RequestProgress from '../src/components/RequestProgress.vue'

afterEach(() => vi.useRealTimers())

describe('真实请求加载反馈', () => {
  it('连续新增请求不会反复推迟加载提示', async () => {
    vi.useFakeTimers()
    const wrapper = mount(RequestProgress)
    const first = {}, second = {}
    startRequest(first)
    await vi.advanceTimersByTimeAsync(80)
    startRequest(second)
    await vi.advanceTimersByTimeAsync(50)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(true)
    endRequest(first)
    endRequest(second)
    wrapper.unmount()
  })
  it('并发请求未全部结束前保持加载,取消和错误也能释放计数', async () => {
    const pending: Array<{ config: any; resolve: (value: any) => void; reject: (error: unknown) => void }> = []
    const adapter = (config: any) => new Promise<any>((resolve, reject) => pending.push({ config, resolve, reject }))
    const first = client.get('/first', { adapter })
    const second = client.get('/second', { adapter }).catch(error => error)
    await flushPromises()
    expect(requestActivity.pending).toBe(2)
    pending[0].resolve({ config: pending[0].config, data: {}, status: 200, statusText: 'OK', headers: {} })
    await first
    expect(requestActivity.pending).toBe(1)
    pending[1].reject(new CanceledError('cancelled', pending[1].config))
    await second
    expect(requestActivity.pending).toBe(0)
    expect(requestActivity.failed).toBe(false)
    const failed = client.get('/failed', { adapter }).catch(error => error)
    await flushPromises()
    pending[2].reject(new AxiosError('network failure', 'ERR_NETWORK', pending[2].config))
    await failed
    expect(requestActivity.pending).toBe(0)
    expect(requestActivity.failed).toBe(true)
  })

  it('短请求不闪烁,慢请求无假百分比,完成后可正确开始下一批', async () => {
    vi.useFakeTimers()
    const wrapper = mount(RequestProgress)
    const fast = {}
    startRequest(fast)
    endRequest(fast)
    await vi.advanceTimersByTimeAsync(130)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    const slow = {}
    startRequest(slow)
    await vi.advanceTimersByTimeAsync(130)
    const bar = wrapper.get('[role="progressbar"]')
    expect(bar.attributes('aria-valuenow')).toBeUndefined()
    endRequest(slow)
    await vi.advanceTimersByTimeAsync(20)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('100')
    const next = {}
    startRequest(next)
    await vi.advanceTimersByTimeAsync(20)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBeUndefined()
    endRequest(next, true)
    await vi.advanceTimersByTimeAsync(20)
    expect(wrapper.text()).toContain('请求未完成')
    await vi.advanceTimersByTimeAsync(700)
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../src/api', () => ({ default: client, errMsg: String }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ replace: vi.fn() }) }))
vi.mock('../src/stores/auth', () => ({ useAuthStore: () => ({ role: 'operator', user: { username: 'test' } }) }))
import ChatView from '../src/views/ChatView.vue'

const wrappers: ReturnType<typeof shallowMount>[] = []
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })

async function chat() {
  client.get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('ai-status') ? { ai_mode: 'local' } : [] }))
  const wrapper = shallowMount(ChatView, { global: { plugins: [ElementPlus] } })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper.vm as any
}

describe('聊天响应归属', () => {
  it('窄屏会话列表可展开和收起，操作具有明确的展开状态', async () => {
    await chat()
    const wrapper = wrappers[wrappers.length - 1]
    const toggle = wrapper.get('.chat-history-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.chat-shell').classes()).toContain('has-history')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
  })
  it('单次输出超过跟随阈值仍跟到底部，用户上翻后暂停，回到底部后恢复', async () => {
    const vm = await chat()
    let height = 1000
    let top = 0
    const el = {
      clientHeight: 400,
      get scrollHeight() { return height },
      get scrollTop() { return top },
      set scrollTop(value: number) { top = Math.min(value, height - 400) },
    }
    vm.scroller = el
    vm.scrollToBottom()
    height = 2400
    vm.maybeScroll()
    expect(top).toBe(2000)
    top = 1200
    vm.onMessageScroll()
    height = 3000
    vm.maybeScroll()
    expect(top).toBe(1200)
    top = 2600
    vm.onMessageScroll()
    height = 3600
    vm.maybeScroll()
    expect(top).toBe(3200)
  })

  it('用户查看历史消息时，回答完成也不强制拉到底部', async () => {
    const vm = await chat()
    const el = { scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 }
    vm.scroller = el
    vm.scrollToBottom()
    el.scrollTop = 500
    vm.onMessageScroll()
    vm.currentId = 1
    vm.stream = { conversationId: 1, content: '回答', tools: [], phase: '', seenUser: true, done: false }
    vm.handleStreamEvent({ type: 'done', title: '', user_message: { id: 10, role: 'user', content: '问题' }, assistant_message: { id: 11, role: 'assistant', content: '回答' } }, vm.stream)
    await flushPromises()
    expect(el.scrollTop).toBe(500)
  })

  it('首段回复前自动显示能量条,开始输出或停止后移除', async () => {
    const vm = await chat()
    const wrapper = wrappers[wrappers.length - 1]
    vm.stream = { conversationId: 1, content: '', tools: [], phase: '检索知识中', seenUser: true, done: false }
    await flushPromises()
    expect(wrapper.find('energy-progress-stub').exists()).toBe(true)
    expect(wrapper.find('energy-progress-stub').attributes('progress')).toBeUndefined()
    vm.stream.content = '首段回复'
    await flushPromises()
    expect(wrapper.find('energy-progress-stub').exists()).toBe(false)
    vm.stream = null
    await flushPromises()
    expect(wrapper.find('energy-progress-stub').exists()).toBe(false)
  })
  it('较晚返回的旧详情不能覆盖新会话', async () => {
    const vm = await chat()
    const pending: Record<string, (value: unknown) => void> = {}
    client.get.mockImplementation((url: string) => new Promise(resolve => { pending[url] = resolve }))
    const first = vm.selectConversation(1)
    const second = vm.selectConversation(2)
    pending['/api/chat/conversations/2']({ data: { messages: [{ id: 20, conversation_id: 2, role: 'user', content: 'B' }], total_messages: 1, has_more: false } })
    await second
    pending['/api/chat/conversations/1']({ data: { messages: [{ id: 10, conversation_id: 1 }], total_messages: 1, has_more: false } })
    await first
    expect(vm.currentId).toBe(2)
    expect(vm.messages.map((row: any) => row.id)).toEqual([20])
  })

  it('旧流的完成事件不能写入当前会话或改名', async () => {
    const vm = await chat()
    vm.currentId = 2
    vm.messages = [{ id: 20, conversation_id: 2, role: 'user', content: 'B' }]
    vm.conversations = [{ id: 2, title: 'B' }]
    vm.handleStreamEvent({ type: 'done', title: 'A', user_message: { id: 10, conversation_id: 1 }, assistant_message: { id: 11, conversation_id: 1 } },
      { conversationId: 1, content: '', tools: [], phase: '', seenUser: true, done: false })
    expect(vm.messages.map((row: any) => row.id)).toEqual([20])
    expect(vm.conversations[0].title).toBe('B')
  })

  it('当前会话的有效流仍正常落到界面', async () => {
    const vm = await chat()
    vm.currentId = 1
    vm.messages = []
    vm.conversations = [{ id: 1, title: 'A' }]
    vm.stream = { conversationId: 1, content: '', tools: [], phase: '', seenUser: false, done: false }
    const state = vm.stream
    vm.handleStreamEvent({ type: 'user_message', message: { id: 10, conversation_id: 1, role: 'user', content: 'question' } }, state)
    vm.handleStreamEvent({ type: 'done', title: '新标题', user_message: { id: 10, conversation_id: 1 }, assistant_message: { id: 11, conversation_id: 1, role: 'assistant', content: 'answer' } }, state)
    expect(vm.messages.map((row: any) => row.id)).toEqual([10, 11])
    expect(vm.conversations[0].title).toBe('新标题')
    expect(state.done).toBe(true)
  })

  it('旧详情失败不能清空已打开的新会话', async () => {
    const vm = await chat()
    let rejectOld!: (reason: unknown) => void
    client.get.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject }))
    const old = vm.selectConversation(1)
    client.get.mockResolvedValueOnce({ data: { messages: [{ id: 20, role: 'user', content: 'B' }], total_messages: 1, has_more: false } })
    await vm.selectConversation(2)
    rejectOld(new Error('old request failed'))
    await old
    expect(vm.messages.map((row: any) => row.id)).toEqual([20])
    expect(vm.error).toBe('')
  })

  it('切换时中止旧流,旧请求退出不能清掉新流状态', async () => {
    const vm = await chat()
    vm.currentId = 1
    let oldSignal!: AbortSignal
    vi.stubGlobal('fetch', vi.fn((_url, options) => {
      oldSignal = options.signal
      return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted'))))
    }))
    const old = vm.submitQuestion('检查 A')
    await flushPromises()
    expect(oldSignal.aborted).toBe(false)
    client.get.mockResolvedValueOnce({ data: { messages: [], total_messages: 0, has_more: false } })
    await vm.selectConversation(2)
    vm.stream = { conversationId: 2, content: 'B 正在生成', tools: [], phase: '', seenUser: true, done: false }
    vm.sending = true
    await old
    expect(oldSignal.aborted).toBe(true)
    expect(vm.sending).toBe(true)
    expect(vm.stream.content).toBe('B 正在生成')
    expect(vm.error).toBe('')
    vi.unstubAllGlobals()
  })
})

<script setup lang="ts">
/**
 * AI 运维对话:左侧会话列表,右侧消息流。
 * 只回答运维问题;不执行任何变更;引用来自已审核知识;答案可人工确认后沉淀。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { fmtTime } from '../labels'
import { renderSafeMd } from '../safeMd'
import { sourcePath, sourceVersionText } from '../sourceLinks'
import { useAuthStore } from '../stores/auth'
import type {
  AiStatus,
  ChatConversation,
  ChatDetail,
  ChatMessageItem,
  ChatToolCall,
  Fault,
} from '../types'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const isAdmin = computed(() => auth.role === 'admin')
const canOperate = computed(() => auth.role === 'operator' || auth.role === 'admin')

const aiStatus = ref<AiStatus | null>(null)
const conversations = ref<ChatConversation[]>([])
const currentId = ref<number | null>(null)
const messages = ref<ChatMessageItem[]>([])
const loading = ref(false)
const loadingMessages = ref(false)
const sending = ref(false)
const error = ref('')
const question = ref('')
const scroller = ref<HTMLElement | null>(null)
const adminView = ref(false)
const currentOwner = ref('')

/** 流式回答的临时状态:delta 累积后实时渲染 Markdown,完成后由正式消息替换。 */
interface StreamState {
  content: string
  tools: ChatToolCall[]
  phase: string
  seenUser: boolean
  done: boolean
}
type StreamEvent =
  | { type: 'user_message'; message: ChatMessageItem }
  | { type: 'status'; text: string }
  | { type: 'delta'; text: string }
  | { type: 'reset' }
  | { type: 'tool'; call: ChatToolCall }
  | { type: 'done'; user_message: ChatMessageItem; assistant_message: ChatMessageItem; title: string }
  | { type: 'error'; code?: string; message: string }

const stream = ref<StreamState | null>(null)
let pendingText = ''
let flushTimer: ReturnType<typeof setTimeout> | null = null

const sediment = reactive({
  open: false,
  mode: 'fault' as 'fault' | 'sop',
  message: null as ChatMessageItem | null,
  faults: [] as Fault[],
  faultId: null as number | null,
  title: '',
  saving: false,
})

const aiDisabled = computed(
  () => !aiStatus.value || (aiStatus.value.ai_mode === 'disabled' && !aiStatus.value.user_ai_enabled),
)
const readOnly = computed(() => adminView.value && currentOwner.value !== auth.user?.username)
const canSend = computed(
  () => !aiDisabled.value && !readOnly.value && !sending.value && question.value.trim().length > 0,
)

function md(text: string): string {
  return renderSafeMd(text || '')
}

async function loadStatus() {
  try {
    const { data } = await client.get<AiStatus>('/api/system/ai-status')
    aiStatus.value = data
  } catch {
    aiStatus.value = null
  }
}

async function loadConversations() {
  loading.value = true
  error.value = ''
  try {
    const url = adminView.value ? '/api/admin/chat/conversations' : '/api/chat/conversations'
    const { data } = await client.get<ChatConversation[]>(url)
    conversations.value = data
    if (!adminView.value && !currentId.value && data.length) {
      await selectConversation(data[0].id)
    }
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

async function selectConversation(id: number) {
  currentId.value = id
  const summary = conversations.value.find((c) => c.id === id)
  currentOwner.value = summary?.username ?? auth.user?.username ?? ''
  loadingMessages.value = true
  error.value = ''
  try {
    const url = adminView.value
      ? `/api/admin/chat/conversations/${id}`
      : `/api/chat/conversations/${id}`
    const { data } = await client.get<ChatDetail>(url)
    messages.value = data.messages
    scrollToBottom()
  } catch (e) {
    messages.value = []
    error.value = errMsg(e)
  } finally {
    loadingMessages.value = false
  }
}

async function newConversation() {
  if (adminView.value) return
  try {
    const { data } = await client.post<ChatConversation>('/api/chat/conversations', { title: '新对话' })
    conversations.value.unshift(data)
    currentId.value = data.id
    messages.value = []
    currentOwner.value = auth.user?.username ?? ''
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function removeConversation(row: ChatConversation) {
  try {
    await ElMessageBox.confirm(`删除会话「${row.title}」及其消息?`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await client.delete(`/api/chat/conversations/${row.id}`)
    conversations.value = conversations.value.filter((c) => c.id !== row.id)
    if (currentId.value === row.id) {
      currentId.value = null
      messages.value = []
      if (conversations.value.length) await selectConversation(conversations.value[0].id)
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function send() {
  if (!canSend.value) return
  const content = question.value.trim()
  question.value = ''
  sending.value = true
  error.value = ''
  pendingText = ''
  const state: StreamState = { content: '', tools: [], phase: '正在连接…', seenUser: false, done: false }
  stream.value = state
  // ref 会把对象包成响应式代理:后续事件必须改代理,改原始对象不会触发渲染
  const live = stream.value as StreamState
  try {
    let conversationId = currentId.value
    if (!conversationId) {
      const { data } = await client.post<ChatConversation>('/api/chat/conversations', { title: '新对话' })
      conversations.value.unshift(data)
      currentId.value = data.id
      currentOwner.value = auth.user?.username ?? ''
      conversationId = data.id
    }
    await nextTick()
    scrollToBottom()
    await streamAsk(conversationId, content, live)
  } catch (e) {
    // 请求未落到服务端(限流/AI 未启用/网络失败)时恢复输入,避免内容丢失
    if (!live.seenUser) question.value = content
    error.value = e instanceof Error ? e.message : errMsg(e)
  } finally {
    sending.value = false
    stream.value = null
    pendingText = ''
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
  }
}

async function streamAsk(conversationId: number, content: string, state: StreamState) {
  const resp = await fetch(`/api/chat/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify({ content }),
  })
  if (!resp.ok) {
    if (resp.status === 401) {
      // fetch 不经过 axios 拦截器,这里保持与全局一致的过期处理
      auth.logout()
      const current = router.currentRoute.value
      if (current.name !== 'login') {
        void router.push({ name: 'login', query: { redirect: current.fullPath } })
      }
      throw new Error('登录会话已过期,请重新登录')
    }
    let detail = `请求失败(HTTP ${resp.status})`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // 非 JSON 错误响应,保留状态码提示
    }
    throw new Error(detail)
  }
  if (!resp.body) throw new Error('当前浏览器不支持流式响应')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const raw = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const event = parseSseEvent(raw)
      if (event) handleStreamEvent(event, state)
      boundary = buffer.indexOf('\n\n')
    }
  }
  buffer += decoder.decode()
  if (buffer.trim()) {
    const event = parseSseEvent(buffer)
    if (event) handleStreamEvent(event, state)
  }
  if (!state.done) {
    throw new Error('回答未完整返回,请重试')
  }
}

function parseSseEvent(raw: string): StreamEvent | null {
  const data = raw
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
  if (!data) return null
  try {
    return JSON.parse(data) as StreamEvent
  } catch {
    return null
  }
}

function handleStreamEvent(event: StreamEvent, state: StreamState) {
  switch (event.type) {
    case 'user_message':
      if (!state.seenUser) {
        state.seenUser = true
        messages.value.push(event.message)
        scrollToBottom()
      }
      break
    case 'status':
      state.phase = event.text
      break
    case 'delta':
      queueText(event.text, state)
      break
    case 'reset':
      // 上一轮文字被判定为工具调用前的铺垫,清空后继续展示最终回答
      pendingText = ''
      state.content = ''
      break
    case 'tool':
      state.tools.push(event.call)
      break
    case 'done': {
      if (!messages.value.some((row) => row.id === event.user_message.id)) {
        messages.value.push(event.user_message)
      }
      messages.value.push(event.assistant_message)
      const summary = conversations.value.find((row) => row.id === currentId.value)
      if (summary && event.title) summary.title = event.title
      stream.value = null
      state.done = true
      void nextTick(() => scrollToBottom())
      break
    }
    case 'error':
      // 服务端已明确拒绝或不可用:抛出后由 send 统一提示并恢复输入
      throw new Error(event.message)
  }
}

function queueText(text: string, state: StreamState) {
  pendingText += text
  if (flushTimer) return
  // 用定时器而不是 requestAnimationFrame:后台/被遮挡的标签页里也能持续刷新
  flushTimer = setTimeout(() => {
    flushTimer = null
    if (!pendingText || stream.value !== state) return
    state.content += pendingText
    pendingText = ''
    maybeScroll()
  }, 40)
}

function maybeScroll() {
  const el = scroller.value
  if (!el) return
  // 用户上翻查看历史时不强行拉回底部
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) {
    el.scrollTop = el.scrollHeight
  }
}

function scrollToBottom() {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}

function toolLabel(tool: string): string {
  if (tool === 'prometheus_query') return 'Prometheus 查询'
  if (tool === 'ssh_run') return 'SSH 只读命令'
  if (tool === 'list_faults') return '故障清单'
  if (tool === 'list_todos') return '待办汇总'
  return tool
}

function toolSummary(call: { tool: string; arguments: Record<string, unknown> }): string {
  if (call.tool === 'prometheus_query') return String(call.arguments.query || '').slice(0, 70)
  if (call.tool === 'ssh_run') return `${call.arguments.server || ''} · ${call.arguments.template || ''}`
  if (call.tool === 'list_faults') {
    const status = String(call.arguments.status || 'unresolved')
    const keyword = call.arguments.keyword ? ` · ${call.arguments.keyword}` : ''
    return `${status}${keyword}`
  }
  if (call.tool === 'list_todos') return '本人待办与待审核'
  return ''
}

function usageText(message: ChatMessageItem): string {
  const u = message.usage
  if (!u) return '用量未知'
  return `用量:输入 ${u.prompt_tokens ?? '未知'} / 输出 ${u.completion_tokens ?? '未知'} / 合计 ${u.total_tokens ?? '未知'}`
}

async function openSediment(message: ChatMessageItem, mode: 'fault' | 'sop') {
  sediment.open = true
  sediment.mode = mode
  sediment.message = message
  sediment.title = message.content.split('\n')[0].replace(/^#+\s*/, '').slice(0, 80) || 'AI 对话沉淀'
  sediment.faultId = null
  if (mode === 'fault') {
    try {
      const { data } = await client.get<Fault[]>('/api/faults')
      sediment.faults = data
    } catch (e) {
      ElMessage.error(errMsg(e))
      sediment.faults = []
    }
  }
}

async function saveSediment() {
  if (!sediment.message) return
  sediment.saving = true
  try {
    if (sediment.mode === 'fault') {
      if (!sediment.faultId) {
        ElMessage.warning('请选择要写入的故障')
        return
      }
      await client.post(`/api/chat/messages/${sediment.message.id}/to-fault-record`, {
        fault_id: sediment.faultId,
      })
      ElMessage.success('已写入故障记录')
      sediment.open = false
    } else {
      const { data } = await client.post(`/api/chat/messages/${sediment.message.id}/to-sop-draft`, {
        title: sediment.title,
      })
      ElMessage.success('已创建 SOP 草稿')
      sediment.open = false
      router.push(`/sops/${data.id}`)
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    sediment.saving = false
  }
}

watch(adminView, async () => {
  currentId.value = null
  messages.value = []
  await loadConversations()
})

onMounted(async () => {
  await Promise.all([loadStatus(), loadConversations()])
  // 从故障页“问 AI”进入:新建会话并预填问题,由模型调用 get_fault 读取详情
  const faultId = Number(route.query.fault)
  if (Number.isSafeInteger(faultId) && faultId > 0 && !adminView.value) {
    await newConversation()
    question.value = `请分析故障 #${faultId} 的当前状态，并给出下一步排查建议。`
    void router.replace({ path: '/chat' })
  }
})
</script>

<template>
  <div class="ops-page chat-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">ASSISTANT / OPERATIONS DIALOGUE</div>
        <h1>AI 对话</h1>
        <p class="ops-page-description">
          交流运维问题、解读日志、梳理排查思路。只回答运维相关内容，不会执行任何命令或变更。
        </p>
      </div>
      <div class="ops-page-actions">
        <label v-if="isAdmin" class="chat-toggle">
          <span>查看全部用户会话</span>
          <el-switch v-model="adminView" />
        </label>
        <el-button v-if="!adminView" type="primary" plain @click="newConversation">
          <AppIcon name="plus" :size="15" />新对话
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="aiDisabled"
      class="mb-5"
      title="AI 未启用"
      description="请先在「AI 设置」中配置自己的 API Key，或由管理员开启全局 AI。"
      type="info"
      :closable="false"
      show-icon
    />

    <div class="chat-shell ops-panel">
      <aside class="chat-list">
        <div class="chat-list-head">
          <span>会话</span>
          <span class="ops-section-caption">{{ conversations.length }}</span>
        </div>
        <div v-if="loading" class="chat-list-empty">加载中…</div>
        <div v-else-if="!conversations.length" class="chat-list-empty">暂无会话</div>
        <button
          v-for="row in conversations"
          :key="row.id"
          type="button"
          class="chat-list-item"
          :class="{ 'is-active': row.id === currentId }"
          @click="selectConversation(row.id)"
        >
          <span class="chat-list-title">{{ row.title || '新对话' }}</span>
          <span class="chat-list-meta">
            {{ row.username ? `${row.username} · ` : '' }}{{ fmtTime(row.updated_at) }}
          </span>
          <span
            v-if="!adminView"
            class="chat-list-delete"
            title="删除会话"
            @click.stop="removeConversation(row)"
          >
            <AppIcon name="close" :size="13" />
          </span>
        </button>
      </aside>

      <section class="chat-main">
        <div v-if="error" class="chat-error">
          <el-alert :title="error" type="error" :closable="false" show-icon />
        </div>

        <div ref="scroller" class="chat-messages">
          <div v-if="!messages.length" class="chat-empty">
            <span class="ops-icon-tile"><AppIcon name="bot" :size="20" /></span>
            <h3>从运维问题开始</h3>
            <p>可以问：有哪些待解决问题、我的故障与待办、本系统案例/SOP、通用运维知识、日志含义、排查步骤、当前监控指标。</p>
            <p class="chat-boundary">不会执行命令或变更；不回答非运维问题；引用只来自你有权查看的已审核知识。</p>
          </div>

          <article
            v-for="message in messages"
            :key="message.id"
            class="chat-message"
            :class="message.role === 'user' ? 'is-user' : 'is-assistant'"
          >
            <div class="chat-avatar">
              <AppIcon :name="message.role === 'user' ? 'user' : 'bot'" :size="16" />
            </div>
            <div class="chat-bubble">
              <div v-if="message.role === 'assistant' && message.status === 'failed'" class="chat-failed">
                <strong>本次回答失败：</strong>{{ message.error_message || message.error_code }}
              </div>
              <div v-else class="chat-md" v-html="md(message.content)"></div>

              <div v-if="message.notes?.length" class="chat-notes">
                <span v-for="(note, index) in message.notes" :key="index">{{ note }}</span>
              </div>

              <div v-if="message.citations?.length" class="chat-citations">
                <span class="chat-citations-label">引用</span>
                <template v-for="citation in message.citations" :key="citation.ref_id">
                  <a
                    v-if="sourcePath(citation.source_type, citation.source_id)"
                    :href="sourcePath(citation.source_type, citation.source_id) || undefined"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="ops-chip is-compact"
                  >
                    {{ citation.source_type === 'sop' ? 'SOP' : '案例' }} {{ citation.ref_id }}
                    <template v-if="sourceVersionText(citation.version)"> · {{ sourceVersionText(citation.version) }}</template>
                  </a>
                  <span v-else class="ops-chip is-compact">{{ citation.ref_id }}（来源无效）</span>
                </template>
              </div>

              <details v-if="message.tool_calls?.length" class="chat-tools">
                <summary>
                  <span>只读查询 {{ message.tool_calls.length }} 次</span>
                  <span class="ops-section-caption">不使用缓存</span>
                </summary>
                <div v-for="(call, index) in message.tool_calls" :key="index" class="chat-tool-row">
                  <div class="chat-tool-head">
                    <strong>{{ toolLabel(call.tool) }}</strong>
                    <span class="ops-id">{{ toolSummary(call) }}</span>
                    <el-tag size="small" :type="call.status === 'ok' ? 'success' : call.status === 'denied' ? 'warning' : 'danger'">
                      {{ call.status }}
                    </el-tag>
                    <span class="ops-section-caption">{{ call.latency_ms }}ms</span>
                  </div>
                  <pre class="chat-tool-output">{{ call.result_excerpt || call.error || '(无输出)' }}</pre>
                </div>
              </details>

              <div v-if="message.role === 'assistant' && message.status === 'ok'" class="chat-actions">
                <span class="chat-meta">{{ usageText(message) }} · {{ message.latency_ms }}ms</span>
                <template v-if="canOperate && !readOnly">
                  <el-button size="small" plain @click="openSediment(message, 'fault')">写入故障记录</el-button>
                  <el-button size="small" plain @click="openSediment(message, 'sop')">生成 SOP 草稿</el-button>
                </template>
              </div>
            </div>
          </article>

          <article v-if="stream" class="chat-message is-assistant is-streaming">
            <div class="chat-avatar">
              <AppIcon name="bot" :size="16" />
            </div>
            <div class="chat-bubble">
              <div v-if="stream.content" class="chat-md" v-html="md(stream.content)"></div>
              <div v-else class="chat-stream-phase">
                <span class="chat-typing"><i></i><i></i><i></i></span>
                {{ stream.phase }}
              </div>
              <div v-if="stream.content" class="chat-stream-phase is-inline">
                <span class="chat-phase-text">{{ stream.phase }}</span>
                <span class="chat-cursor" aria-hidden="true"></span>
              </div>

              <details v-if="stream.tools.length" class="chat-tools" open>
                <summary>
                  <span>只读查询 {{ stream.tools.length }} 次</span>
                  <span class="ops-section-caption">不使用缓存</span>
                </summary>
                <div v-for="(call, index) in stream.tools" :key="index" class="chat-tool-row">
                  <div class="chat-tool-head">
                    <strong>{{ toolLabel(call.tool) }}</strong>
                    <span class="ops-id">{{ toolSummary(call) }}</span>
                    <el-tag size="small" :type="call.status === 'ok' ? 'success' : call.status === 'denied' ? 'warning' : 'danger'">
                      {{ call.status }}
                    </el-tag>
                    <span class="ops-section-caption">{{ call.latency_ms }}ms</span>
                  </div>
                  <pre class="chat-tool-output">{{ call.result_excerpt || call.error || '(无输出)' }}</pre>
                </div>
              </details>
            </div>
          </article>
        </div>

        <div v-if="!adminView" class="chat-composer">
          <el-input
            v-model="question"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="8000"
            show-word-limit
            :placeholder="aiDisabled ? 'AI 未启用，无法提问' : '描述你的运维问题，Enter 发送，Shift+Enter 换行'"
            :disabled="aiDisabled || sending"
            @keydown="onKeydown"
          />
          <div class="chat-composer-foot">
            <span class="ops-section-caption">只回答运维问题；不会执行命令或访问无权数据。</span>
            <el-button type="primary" :loading="sending" :disabled="!canSend" @click="send">
              发送
            </el-button>
          </div>
        </div>
        <div v-else class="chat-readonly">
          管理员只读查看模式；如需提问请关闭「查看全部用户会话」。
        </div>
      </section>
    </div>

    <el-dialog v-model="sediment.open" :title="sediment.mode === 'fault' ? '写入故障记录' : '生成 SOP 草稿'" width="520px" append-to-body>
      <template v-if="sediment.mode === 'fault'">
        <label class="form-label">选择故障</label>
        <el-select v-model="sediment.faultId" class="w-full" placeholder="选择要写入的故障">
          <el-option v-for="fault in sediment.faults" :key="fault.id" :label="`#${fault.id} ${fault.title}`" :value="fault.id" />
        </el-select>
        <p class="chat-boundary mt-2">将以「处理记录」追加到该故障，不修改故障状态或根因。</p>
      </template>
      <template v-else>
        <label class="form-label">SOP 标题</label>
        <el-input v-model="sediment.title" maxlength="255" />
        <p class="chat-boundary mt-2">将创建一个草稿，正文为本次回答；提交审核与发布仍需人工操作。</p>
      </template>
      <template #footer>
        <el-button @click="sediment.open = false">取消</el-button>
        <el-button type="primary" :loading="sediment.saving" @click="saveSediment">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
}
.chat-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--ops-text-soft);
  font-size: var(--ops-text-xs);
  white-space: nowrap;
}
.chat-shell {
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  min-height: 620px;
  height: calc(100vh - 220px);
}
.chat-list {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-right: 1px solid var(--ops-border-light);
  background: var(--ops-surface-muted);
}
.chat-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--ops-border-light);
  color: var(--ops-muted);
  font-size: 11px;
}
.chat-list-empty {
  padding: 20px 16px;
  color: var(--ops-muted);
  font-size: 12px;
}
.chat-list-item {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 11px 30px 11px 16px;
  border: 0;
  border-bottom: 1px solid var(--ops-border-light);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.chat-list-item:hover {
  background: var(--ops-surface);
}
.chat-list-item.is-active {
  background: var(--ops-surface);
  box-shadow: inset 2px 0 0 var(--ops-brand);
}
.chat-list-title {
  overflow: hidden;
  color: var(--ops-text);
  font-size: 12px;
  font-weight: var(--ops-weight-medium);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-list-meta {
  color: var(--ops-muted);
  font-family: var(--ops-font-mono);
  font-size: 9px;
}
.chat-list-delete {
  position: absolute;
  top: 8px;
  right: 8px;
  display: none;
  padding: 3px;
  color: var(--ops-muted);
  cursor: pointer;
}
.chat-list-item:hover .chat-list-delete {
  display: inline-flex;
}
.chat-main {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.chat-error {
  padding: 12px 16px 0;
}
.chat-messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px 22px;
}
.chat-empty {
  max-width: 520px;
  margin: 60px auto;
  color: var(--ops-muted);
  text-align: left;
}
.chat-empty h3 {
  margin: 12px 0 6px;
  color: var(--ops-text);
  font-size: 16px;
}
.chat-empty p {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.7;
}
.chat-boundary {
  color: var(--ops-faint);
  font-size: 11px;
  line-height: 1.7;
}
.chat-message {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
}
.chat-message.is-user {
  flex-direction: row-reverse;
}
.chat-avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border: 1px solid var(--ops-border);
  border-radius: 4px;
  background: var(--ops-surface);
  color: var(--ops-brand);
}
.chat-message.is-user .chat-avatar {
  color: var(--ops-muted);
}
.chat-bubble {
  max-width: min(760px, 82%);
  padding: 12px 14px;
  border: 1px solid var(--ops-border-light);
  border-radius: var(--ops-radius-control);
  background: var(--ops-surface);
}
.chat-message.is-user .chat-bubble {
  background: var(--ops-surface-warm);
}
.chat-md {
  color: var(--ops-text-soft);
  font-size: 12px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.chat-md :deep(p) {
  margin: 0 0 8px;
}
.chat-md :deep(pre) {
  padding: 10px;
  overflow-x: auto;
  border-radius: 4px;
  background: var(--ops-surface-muted);
  font-family: var(--ops-font-mono);
  font-size: 11px;
}
.chat-md :deep(code) {
  font-family: var(--ops-font-mono);
  font-size: 11px;
}
.chat-message.is-streaming .chat-bubble {
  border-style: dashed;
}
.chat-stream-phase {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ops-muted);
  font-size: 11px;
}
.chat-stream-phase.is-inline {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--ops-border-light);
}
.chat-phase-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-typing {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.chat-typing i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--ops-brand);
  animation: chat-blink 1.1s infinite ease-in-out;
}
.chat-typing i:nth-child(2) {
  animation-delay: 0.15s;
}
.chat-typing i:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes chat-blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  40% {
    opacity: 1;
    transform: translateY(-1px);
  }
}
.chat-cursor {
  display: inline-block;
  width: 2px;
  height: 12px;
  background: var(--ops-brand);
  animation: chat-caret 1s steps(1) infinite;
}
@keyframes chat-caret {
  50% {
    opacity: 0;
  }
}
.chat-failed {
  color: var(--ops-danger);
  font-size: 12px;
  line-height: 1.7;
}
.chat-notes {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 8px;
  color: var(--ops-warning, #b07d2b);
  font-size: 10px;
}
.chat-citations {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
}
.chat-citations-label {
  color: var(--ops-muted);
  font-size: 10px;
}
.chat-tools {
  margin-top: 10px;
  border-top: 1px solid var(--ops-border-light);
  padding-top: 8px;
}
.chat-tools summary {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--ops-text-soft);
  font-size: 11px;
  cursor: pointer;
  list-style: none;
}
.chat-tools summary::-webkit-details-marker {
  display: none;
}
.chat-tool-row {
  margin-top: 8px;
}
.chat-tool-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}
.chat-tool-output {
  margin: 6px 0 0;
  padding: 8px;
  max-height: 200px;
  overflow: auto;
  border-radius: 4px;
  background: var(--ops-surface-muted);
  color: var(--ops-text-soft);
  font-family: var(--ops-font-mono);
  font-size: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.chat-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
}
.chat-meta {
  color: var(--ops-faint);
  font-family: var(--ops-font-mono);
  font-size: 9px;
}
.chat-composer {
  padding: 12px 16px;
  border-top: 1px solid var(--ops-border-light);
  background: var(--ops-surface-muted);
}
.chat-composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}
.chat-readonly {
  padding: 14px 16px;
  border-top: 1px solid var(--ops-border-light);
  background: var(--ops-surface-muted);
  color: var(--ops-muted);
  font-size: 11px;
}
@media (max-width: 900px) {
  .chat-shell {
    grid-template-columns: 1fr;
    height: auto;
  }
  .chat-list {
    max-height: 220px;
    overflow-y: auto;
    border-right: 0;
    border-bottom: 1px solid var(--ops-border-light);
  }
  .chat-messages {
    min-height: 360px;
  }
}
</style>
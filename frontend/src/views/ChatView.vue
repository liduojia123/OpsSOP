<script setup lang="ts">
/**
 * AI 运维对话:左侧会话列表,右侧消息流。
 * 只回答运维问题;不执行任何变更;引用来自已审核知识;答案可人工确认后沉淀。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { fmtTime } from '../labels'
import { renderSafeMd } from '../safeMd'
import { sourcePath, sourceVersionText } from '../sourceLinks'
import { useAuthStore } from '../stores/auth'
import type {
  AiStatus,
  ChatAskResponse,
  ChatConversation,
  ChatDetail,
  ChatMessageItem,
  Fault,
} from '../types'

const auth = useAuthStore()
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
  try {
    if (!currentId.value) {
      const { data } = await client.post<ChatConversation>('/api/chat/conversations', { title: '新对话' })
      conversations.value.unshift(data)
      currentId.value = data.id
      currentOwner.value = auth.user?.username ?? ''
    }
    const { data } = await client.post<ChatAskResponse>(
      `/api/chat/conversations/${currentId.value}/messages`,
      { content },
      { timeout: 300000 },
    )
    messages.value.push(data.user_message, data.assistant_message)
    const summary = conversations.value.find((c) => c.id === currentId.value)
    if (summary && (summary.title === '新对话' || !summary.title)) {
      summary.title = content.slice(0, 40)
    }
    await nextTick()
    scrollToBottom()
  } catch (e) {
    question.value = content // 失败不丢输入
    error.value = errMsg(e)
  } finally {
    sending.value = false
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
  return tool
}

function toolSummary(call: { tool: string; arguments: Record<string, unknown> }): string {
  if (call.tool === 'prometheus_query') return String(call.arguments.query || '').slice(0, 70)
  if (call.tool === 'ssh_run') return `${call.arguments.server || ''} · ${call.arguments.template || ''}`
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
            <p>可以问：本系统相关案例/SOP、通用运维知识、日志含义、排查步骤、当前监控指标。</p>
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
                  <span>实时查询 {{ message.tool_calls.length }} 次</span>
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
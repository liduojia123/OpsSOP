<script setup lang="ts">
/**
 * 安全 Markdown 渲染 + 代码块复制按钮。
 * - 内容经 renderSafeMd 全量转义后 v-html,不透传原始 HTML;
 * - 渲染后为每个代码块注入「复制」按钮:流式输出会整体重建 innerHTML,
 *   因此每次内容变化后在 nextTick 重新补齐缺失的按钮;
 * - 点击用事件代理,复制取 <code> 文本(不含按钮自身);剪贴板 API 不可用时回退 execCommand。
 */
import { nextTick, onMounted, ref, watch } from 'vue'
import { renderSafeMd } from '../safeMd'

const props = defineProps<{ content: string }>()
const root = ref<HTMLElement | null>(null)

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 非安全上下文或权限被拒,走回退方案
  }
  try {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}

function enhance() {
  const el = root.value
  if (!el) return
  el.querySelectorAll('pre.md-code').forEach((node) => {
    const pre = node as HTMLElement
    if (pre.querySelector(':scope > .md-copy-btn')) return
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'md-copy-btn'
    btn.textContent = '复制'
    pre.appendChild(btn)
  })
}

async function onClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  const btn = target.closest('.md-copy-btn') as HTMLElement | null
  if (!btn || !btn.parentElement) return
  const pre = btn.parentElement
  const code = pre.querySelector('code')
  const text = (code?.textContent ?? pre.textContent ?? '').replace(/\n$/, '')
  const ok = await copyText(text)
  btn.textContent = ok ? '已复制' : '复制失败'
  const timer = Number(btn.dataset.timer || 0)
  if (timer) clearTimeout(timer)
  btn.dataset.timer = String(setTimeout(() => (btn.textContent = '复制'), 1200))
}

onMounted(enhance)
watch(
  () => props.content,
  () => void nextTick(enhance),
)
</script>

<template>
  <div ref="root" class="chat-md" @click="onClick" v-html="renderSafeMd(content || '')"></div>
</template>

<style scoped>
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
  position: relative;
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
.chat-md :deep(.md-copy-btn) {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 8px;
  border: 1px solid var(--ops-border);
  border-radius: 3px;
  background: var(--ops-surface);
  color: var(--ops-muted);
  cursor: pointer;
  font-size: 10px;
  line-height: 1.4;
}
.chat-md :deep(.md-copy-btn:hover) {
  border-color: var(--ops-brand);
  color: var(--ops-brand);
}
</style>

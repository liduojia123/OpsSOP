<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { requestActivity } from '../requestActivity'
import EnergyProgress from './EnergyProgress.vue'

const visible = ref(false)
const phase = ref<'loading' | 'done' | 'error'>('loading')
let showTimer: ReturnType<typeof setTimeout> | undefined
let hideTimer: ReturnType<typeof setTimeout> | undefined

watch(() => requestActivity.pending > 0, (pending) => {
  clearTimeout(showTimer)
  clearTimeout(hideTimer)
  if (pending) {
    phase.value = 'loading'
    if (!visible.value) showTimer = setTimeout(() => { visible.value = true }, 120)
  } else if (visible.value) {
    phase.value = requestActivity.failed ? 'error' : 'done'
    hideTimer = setTimeout(() => { visible.value = false }, 600)
  }
}, { immediate: true })

onBeforeUnmount(() => { clearTimeout(showTimer); clearTimeout(hideTimer) })
</script>

<template>
  <div v-if="visible" class="ops-request-progress" :class="'is-' + phase" role="progressbar"
    :aria-label="phase === 'loading' ? '正在加载页面数据' : phase === 'error' ? '请求未完成' : '数据已更新'"
    :aria-valuenow="phase === 'done' ? 100 : undefined">
    <span class="ops-request-track"><i></i></span>
    <div class="ops-request-caption" aria-hidden="true">
      <span>{{ phase === 'loading' ? '正在连接数据' : phase === 'error' ? '请求未完成' : '数据已更新' }}</span>
      <EnergyProgress compact :phase="phase" :label="phase === 'loading' ? '正在连接数据' : phase === 'error' ? '请求未完成' : '数据已更新'" />
    </div>
  </div>
</template>

<style scoped>
.ops-request-progress { position: fixed; inset: 0 0 auto; z-index: 3000; pointer-events: none; color: var(--ops-brand); }
.ops-request-track { display: block; height: 2px; overflow: hidden; background: var(--ops-brand-soft); }
.ops-request-track > i { display: block; width: 34%; height: 100%; background: linear-gradient(90deg, transparent, currentColor 40%, currentColor); animation: ops-request-sweep 1.2s cubic-bezier(.4,0,.2,1) infinite; }
.ops-request-caption { position: absolute; top: 76px; right: 24px; width: min(290px, calc(100vw - 32px)); padding: 12px; border: 1px solid var(--ops-border); border-radius: 10px; background: var(--ops-surface); box-shadow: var(--ops-shadow-raised); }
.ops-request-caption > span { display: block; margin-bottom: 8px; color: var(--ops-text-soft); font-size: 12px; }
.ops-request-caption :deep(.ops-energy.is-compact) { height: 18px; border-radius: 5px; }
.ops-request-caption :deep(.ops-energy-copy) { display: none; }
.is-done { color: var(--ops-success); }
.is-error { color: var(--ops-danger); }
.is-done .ops-request-track > i, .is-error .ops-request-track > i { width: 100%; animation: none; background: currentColor; }
@keyframes ops-request-sweep { from { transform: translateX(-110%); } to { transform: translateX(400%); } }
@media (prefers-reduced-motion: reduce) { .ops-request-track > i { animation: none; width: 100%; opacity: .6; } }
</style>

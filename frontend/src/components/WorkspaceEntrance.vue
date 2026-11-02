<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppLogo from './AppLogo.vue'
import { workspaceEntrance, cancelWorkspaceEntrance } from '../workspaceEntrance'

const route = useRoute()
const overlay = ref<HTMLElement | null>(null)
const active = computed(() => workspaceEntrance.phase !== 'idle')
const reduced = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

function finish() {
  if (!active.value) return
  cancelWorkspaceEntrance()
  void nextTick(() => {
    if (route.name !== 'login') document.querySelector<HTMLElement>('.ops-main')?.focus({ preventScroll: true })
  })
}
function onDoorEnd(event: AnimationEvent) {
  if (event.target === event.currentTarget && workspaceEntrance.phase === 'opening') finish()
}
watch(() => workspaceEntrance.phase, phase => {
  clearTimeout(timer)
  if (phase === 'idle') return
  reduced.value = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  // 路由加载或浏览器动画事件异常时，也必须释放背景交互。
  timer = setTimeout(finish, phase === 'covered' ? 8000 : reduced.value ? 180 : 1400)
  if (phase === 'covered') void nextTick(() => overlay.value?.focus({ preventScroll: true }))
}, { flush: 'sync', immediate: true })
watch(() => route.name, name => { if (name === 'login') cancelWorkspaceEntrance() })
onBeforeUnmount(() => { clearTimeout(timer); cancelWorkspaceEntrance() })
</script>

<template>
  <div v-if="active" ref="overlay" class="ops-workspace-entrance" :class="{ 'is-opening': workspaceEntrance.phase === 'opening', 'is-reduced': reduced }" tabindex="-1" role="status" aria-label="登录成功，正在进入工作空间">
    <div class="ops-entrance-half ops-entrance-left" aria-hidden="true">
      <div class="ops-entrance-brand"><AppLogo icon-only /></div>
    </div>
    <div class="ops-entrance-half ops-entrance-right" aria-hidden="true" @animationend="onDoorEnd">
      <div class="ops-entrance-brand"><AppLogo icon-only /></div>
    </div>
    <p v-if="workspaceEntrance.phase === 'covered'" class="ops-entrance-preparing">正在准备工作空间</p>
    <button v-else class="ops-entrance-skip" @click="finish">跳过动画</button>
  </div>
</template>

<style scoped>
.ops-workspace-entrance {
  position: fixed; inset: 0; z-index: 4000; overflow: hidden; outline: none;
  --entrance-logo-size: clamp(240px, 32vw, 420px);
  /* 折线的两翼沿图形内部斜向留白，中段反折连接，坐标随标志大小同步。 */
  --cut-ax: calc(50% - var(--entrance-logo-size) * .5);
  --cut-ay: calc(50% - var(--entrance-logo-size) * .1);
  --cut-bx: calc(50% - var(--entrance-logo-size) * .17);
  --cut-by: calc(50% + var(--entrance-logo-size) * .1);
  --cut-cx: calc(50% + var(--entrance-logo-size) * .17);
  --cut-cy: calc(50% - var(--entrance-logo-size) * .1);
  --cut-dx: calc(50% + var(--entrance-logo-size) * .5);
  --cut-dy: calc(50% + var(--entrance-logo-size) * .1);
  --cut-entry: calc(50% - 28.8vw + var(--entrance-logo-size) * .188);
  --cut-exit: calc(50% + 28.8vw - var(--entrance-logo-size) * .188);
}
.ops-entrance-half { position: absolute; inset: 0; overflow: hidden; background-color: var(--ops-surface); background-image: radial-gradient(#c94b2514 .6px, transparent .6px); background-size: 24px 24px; will-change: transform; }
.ops-entrance-left { clip-path: polygon(0 var(--cut-entry), var(--cut-ax) var(--cut-ay), var(--cut-bx) var(--cut-by), var(--cut-cx) var(--cut-cy), var(--cut-dx) var(--cut-dy), 100% var(--cut-exit), 100% 200vmax, 0 200vmax); }
.ops-entrance-right { clip-path: polygon(0 -200vmax, 100% -200vmax, 100% var(--cut-exit), var(--cut-dx) var(--cut-dy), var(--cut-cx) var(--cut-cy), var(--cut-bx) var(--cut-by), var(--cut-ax) var(--cut-ay), 0 var(--cut-entry)); }
.ops-entrance-brand { position: absolute; top: 50%; left: 50%; width: var(--entrance-logo-size); transform: translate(-50%, -50%); --ops-logo-icon-size: var(--entrance-logo-size); }
.ops-entrance-brand :deep(.ops-logo) { max-width: none; animation: ops-logo-arrive 200ms ease-out both; }
.ops-entrance-preparing { position: absolute; top: calc(50% + var(--entrance-logo-size) * .5 + 28px); left: 0; right: 0; margin: 0; text-align: center; color: var(--ops-muted); font-size: 12px; }
.ops-entrance-skip { position: absolute; right: 28px; bottom: 23px; padding: 8px; border: 0; border-radius: 4px; color: var(--ops-muted); background: var(--ops-surface); cursor: pointer; font-size: 11px; }
.ops-entrance-skip:hover { color: var(--ops-brand); }
.is-opening .ops-entrance-left { animation: ops-door-left 920ms cubic-bezier(.76,0,.24,1) 220ms both; }
.is-opening .ops-entrance-right { animation: ops-door-right 920ms cubic-bezier(.76,0,.24,1) 220ms both; }
@keyframes ops-logo-arrive { from { opacity: 0; } to { opacity: 1; } }
@keyframes ops-door-left { from { transform: translate3d(0, 0, 0); } to { transform: translate3d(-60vmax, 105vmax, 0); } }
@keyframes ops-door-right { from { transform: translate3d(0, 0, 0); } to { transform: translate3d(60vmax, -105vmax, 0); } }
.is-reduced.is-opening { animation: ops-door-fade 160ms ease-out both; }
.is-reduced .ops-entrance-half { animation: none; }
@keyframes ops-door-fade { to { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .ops-entrance-half, .ops-entrance-brand :deep(.ops-logo) { animation: none !important; } }
@media (max-width: 560px) { .ops-entrance-skip { right: 16px; bottom: 16px; } }
</style>

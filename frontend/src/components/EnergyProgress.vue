<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'

const props = withDefaults(defineProps<{
  progress?: number
  phase?: 'loading' | 'done' | 'error'
  label?: string
  subtitle?: string
  statusText?: string
  running?: boolean
  speed?: number
  amplitude?: number
  compact?: boolean
}>(), { phase: 'loading', label: '正在连接数据', subtitle: '等待服务响应', statusText: '连接中', running: true, speed: 1, amplitude: 1, compact: false })

const id = `energy-${useId()}`
const time = ref(3)
const reduced = ref(false)
const hidden = ref(false)
let media: MediaQueryList | undefined
let frame = 0
let previous = 0
const progress = computed(() => props.phase === 'done' ? 100 : typeof props.progress === 'number' && Number.isFinite(props.progress) ? Math.min(100, Math.max(0, props.progress)) : undefined)
const edge = computed(() => progress.value === undefined
  ? props.phase === 'error' || reduced.value ? 580 : (time.value * 95 % 1480) - 120
  : progress.value === 100 ? 1030 : progress.value === 0 ? -30 : progress.value * 10)
const front = computed(() => {
  const amplitude = props.phase === 'loading' && !reduced.value ? props.amplitude : 0
  return Array.from({ length: 37 }, (_, i) => {
    const y = i * 5
    const x = edge.value + amplitude * (Math.sin(y * .083 + time.value * 4) * 5 + Math.sin(y * .21 - time.value * 6) * 2.8 + Math.sin(y * .51 + time.value * 3) * 1.5)
    return `${i ? 'L' : 'M'}${x.toFixed(2)},${y}`
  }).join(' ')
})
const fill = computed(() => `${front.value} L${progress.value === undefined ? edge.value - 460 : -40},180 L${progress.value === undefined ? edge.value - 460 : -40},0 Z`)
const percent = computed(() => progress.value === undefined ? props.phase === 'error' ? '已中断' : props.statusText : `${Math.round(progress.value)}%`)

function stop() { cancelAnimationFrame(frame); frame = 0; previous = 0 }
function draw(now: number) {
  if (previous && now - previous >= 33) {
    time.value += Math.min(now - previous, 100) / 1000 * props.speed
    previous = now
  } else if (!previous) previous = now
  frame = requestAnimationFrame(draw)
}
function syncAnimation() {
  stop()
  if (props.running && props.phase === 'loading' && !reduced.value && !hidden.value) frame = requestAnimationFrame(draw)
}
function syncMotion() { reduced.value = media?.matches ?? false }
function syncVisibility() { hidden.value = document.hidden }
watch(() => [props.running, props.phase, reduced.value, hidden.value], syncAnimation)
onMounted(() => {
  if (typeof window.matchMedia === 'function') {
    media = window.matchMedia('(prefers-reduced-motion: reduce)')
    syncMotion()
    media.addEventListener('change', syncMotion)
  }
  syncVisibility()
  document.addEventListener('visibilitychange', syncVisibility)
  syncAnimation()
})
onBeforeUnmount(() => {
  stop()
  media?.removeEventListener('change', syncMotion)
  document.removeEventListener('visibilitychange', syncVisibility)
})
</script>

<template>
  <div class="ops-energy" :class="['is-' + phase, { 'is-compact': compact }]" role="progressbar" :aria-label="label" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100">
    <svg viewBox="0 0 1000 180" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient :id="id + '-fill'"><stop offset="0" stop-color="var(--energy-start)" /><stop offset=".3" stop-color="var(--energy-mid)" /><stop offset=".83" stop-color="var(--energy-deep)" /><stop offset="1" stop-color="var(--energy-front)" /></linearGradient>
        <linearGradient :id="id + '-shine'" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff" stop-opacity=".18" /><stop offset=".5" stop-color="#fff" stop-opacity="0" /><stop offset="1" stop-color="#fff" stop-opacity=".08" /></linearGradient>
        <clipPath :id="id + '-clip'"><path :d="fill" /></clipPath>
        <filter :id="id + '-glow'" x="-100%" y="-30%" width="300%" height="160%"><feGaussianBlur stdDeviation="7" /></filter>
      </defs>
      <rect width="1000" height="180" fill="var(--ops-brand-soft)" />
      <g class="ops-energy-copy"><text x="58" y="85">{{ label }}</text><text v-if="!compact" x="60" y="113" class="ops-energy-subtitle">{{ subtitle }}</text><text x="942" y="104" text-anchor="end" class="ops-energy-percent">{{ percent }}</text></g>
      <g :clip-path="`url(#${id}-clip)`">
        <rect :x="progress === undefined ? edge - 460 : 0" width="1000" height="180" :fill="`url(#${id}-fill)`" />
        <rect width="1000" height="180" :fill="`url(#${id}-shine)`" />
        <g class="ops-energy-copy is-filled"><text x="58" y="85">{{ label }}</text><text v-if="!compact" x="60" y="113" class="ops-energy-subtitle">{{ subtitle }}</text><text x="942" y="104" text-anchor="end" class="ops-energy-percent">{{ percent }}</text></g>
      </g>
      <g v-if="phase === 'loading'" class="ops-energy-light">
        <path :d="front" :filter="`url(#${id}-glow)`" stroke="var(--ops-brand-mark)" stroke-width="20" opacity=".7" fill="none" />
        <path :d="front" stroke="#fff1d6" stroke-width="2.5" fill="none" />
        <circle v-for="i in 10" :key="i" :cx="edge - 13 - ((i * 27 + time * 23) % 110)" :cy="12 + ((i * 37 + time * (i % 2 ? 7 : -9) + 1800) % 156)" :r="i % 3 === 0 ? 1.8 : .9" fill="#fff3d9" :opacity="reduced ? 0 : .3 + (Math.sin(time * 3 + i) + 1) * .25" />
      </g>
    </svg>
  </div>
</template>

<style scoped>
.ops-energy { --energy-start: var(--ops-brand-pressed); --energy-mid: var(--ops-brand); --energy-deep: var(--ops-brand-hover); --energy-front: var(--ops-brand-mark); width: 100%; height: 108px; overflow: hidden; isolation: isolate; border: 1px solid var(--ops-brand-line); border-radius: 22px; background: var(--ops-brand-soft); box-shadow: 0 8px 30px rgb(201 75 37 / 4%); }
.ops-energy svg { display: block; width: 100%; height: 100%; }
.ops-energy-copy { fill: var(--ops-text); font: 600 32px var(--ops-font-body); letter-spacing: -.7px; }
.ops-energy-subtitle { font-size: 16px; letter-spacing: 1px; opacity: .75; }
.ops-energy-percent { font-size: 48px; font-variant-numeric: tabular-nums; letter-spacing: -1px; }
.ops-energy-copy.is-filled { fill: white; }
.ops-energy.is-compact { height: 62px; border-radius: 13px; }
.is-compact .ops-energy-copy { font-size: 38px; }
.is-compact .ops-energy-copy > text:first-child { transform: translateY(12px); }
.is-compact .ops-energy-percent { font-size: 38px; }
.is-done { --energy-start: #55b9a0; --energy-mid: #10886e; --energy-deep: #08745d; --energy-front: #50c7b5; }
.is-error { --energy-start: #efb3a9; --energy-mid: #cf6257; --energy-deep: #b84742; --energy-front: #ef9188; }
@media (max-width: 560px) { .ops-energy { height: 92px; border-radius: 16px; } .ops-energy.is-compact { height: 54px; } }
</style>

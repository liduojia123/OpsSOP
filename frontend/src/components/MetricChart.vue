<script setup lang="ts">
/**
 * 轻量 SVG 折线图(不引入图表库)。
 * 输入为 Prometheus range 结果:每条序列一组 [timestamp, value] 点。
 */
import { computed } from 'vue'

interface Series {
  labels: Record<string, string>
  points: number[][]
}

const props = withDefaults(
  defineProps<{
    series: Series[]
    unit?: string
    height?: number
  }>(),
  { unit: '', height: 150 },
)

const WIDTH = 640
const PADDING = 8

const palette = ['#c94b25', '#3f6f5a', '#7a5aa6', '#b07d2b', '#4c6f9c']

function instanceLabel(labels: Record<string, string>): string {
  return labels.host || labels.instance || labels.job || 'value'
}

const prepared = computed(() => {
  const all = props.series.flatMap((s) => s.points.map((p) => Number(p[1])))
  const finite = all.filter((v) => Number.isFinite(v))
  if (!finite.length) return { paths: [], min: 0, max: 0 }
  let min = Math.min(...finite)
  let max = Math.max(...finite)
  if (props.unit === 'percent') {
    min = Math.min(min, 0)
    max = Math.max(max, 100)
  }
  if (max - min < 1e-9) {
    max += 1
    min -= 1
  }
  const span = max - min
  const innerH = props.height - PADDING * 2
  const paths = props.series
    .filter((s) => s.points.length > 0)
    .map((s, index) => {
      const count = s.points.length
      const stepX = count > 1 ? WIDTH / (count - 1) : WIDTH
      const d = s.points
        .map((point, i) => {
          const x = (i * stepX).toFixed(1)
          const y = (PADDING + innerH - ((Number(point[1]) - min) / span) * innerH).toFixed(1)
          return `${i === 0 ? 'M' : 'L'}${x},${y}`
        })
        .join(' ')
      return {
        d,
        color: palette[index % palette.length],
        name: instanceLabel(s.labels),
        last: Number(s.points[s.points.length - 1][1]),
      }
    })
  return { paths, min, max }
})

const gridLines = computed(() => {
  const innerH = props.height - PADDING * 2
  return [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    y: PADDING + innerH * ratio,
    value: prepared.value.max - (prepared.value.max - prepared.value.min) * ratio,
  }))
})

function fmt(value: number): string {
  if (!Number.isFinite(value)) return '—'
  if (props.unit === 'percent') return `${value.toFixed(1)}%`
  if (props.unit === 'bytes') {
    if (Math.abs(value) > 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB/s`
    if (Math.abs(value) > 1024) return `${(value / 1024).toFixed(1)} KB/s`
    return `${value.toFixed(0)} B/s`
  }
  if (props.unit === 'load') return value.toFixed(2)
  return value.toFixed(2)
}
</script>

<template>
  <div class="metric-chart">
    <svg :viewBox="`0 0 ${WIDTH} ${height}`" preserveAspectRatio="none" role="img">
      <line
        v-for="(line, index) in gridLines"
        :key="index"
        :x1="0"
        :x2="WIDTH"
        :y1="line.y"
        :y2="line.y"
        class="metric-grid-line"
      />
      <path v-for="path in prepared.paths" :key="path.name" :d="path.d" :stroke="path.color" class="metric-line" />
    </svg>
    <div class="metric-legend">
      <span v-for="path in prepared.paths" :key="path.name">
        <i :style="{ background: path.color }"></i>{{ path.name }} · {{ fmt(path.last) }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.metric-chart {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
svg {
  width: 100%;
  height: auto;
  display: block;
}
.metric-grid-line {
  stroke: var(--ops-border-light);
  stroke-width: 1;
  stroke-dasharray: 2 5;
}
.metric-line {
  fill: none;
  stroke-width: 1.8;
  vector-effect: non-scaling-stroke;
}
.metric-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  color: var(--ops-muted);
  font-family: var(--ops-font-mono);
  font-size: 10px;
}
.metric-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.metric-legend i {
  width: 7px;
  height: 7px;
  border-radius: 2px;
}
</style>
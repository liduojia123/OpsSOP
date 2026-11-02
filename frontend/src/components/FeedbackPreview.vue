<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import EnergyProgress from './EnergyProgress.vue'
import { startRequest, endRequest } from '../requestActivity'

const open = defineModel<boolean>({ default: false })
const phase = ref<'idle' | 'loading' | 'done' | 'error'>('idle')
const progress = ref(42)
const speed = ref(1)
const amplitude = ref(.85)
let timer: ReturnType<typeof setInterval> | undefined
let activity: object | undefined

function cancel() {
  clearInterval(timer)
  endRequest(activity)
  activity = undefined
  phase.value = 'idle'
}
function demonstrate(failed = false) {
  cancel()
  phase.value = 'loading'
  progress.value = 0
  activity = {}
  startRequest(activity)
  timer = setInterval(() => {
    progress.value = Math.min(failed ? 66 : 100, progress.value + 40 / 3600 * 100 * speed.value)
    if (progress.value >= (failed ? 66 : 100)) {
      clearInterval(timer)
      endRequest(activity, failed)
      activity = undefined
      phase.value = failed ? 'error' : 'done'
    }
  }, 40)
}
watch(open, value => { if (!value) cancel() })
onBeforeUnmount(cancel)
</script>

<template>
  <el-dialog v-model="open" title="能量加载 · 动效预览" width="min(600px, 94vw)" class="ops-feedback-dialog">
    <p class="ops-feedback-intro">按参考视频还原渐变填充、波动发光前沿与微粒。此处进度为演示，可调节速度与波幅，不发送请求或保存数据。</p>
    <div class="ops-energy-demo">
      <EnergyProgress :progress="progress" :phase="phase === 'idle' ? 'loading' : phase" :speed="speed" :amplitude="amplitude" label="同步知识库 · 演示" :subtitle="phase === 'error' ? '演示异常 · 可重新体验' : phase === 'done' ? '演示完成' : '正在准备你的工作空间'" :running="open" />
      <p class="ops-energy-state" role="status">{{ phase === 'done' ? '已完成 · 演示' : phase === 'error' ? '连接未完成 · 演示' : phase === 'loading' ? '加载中 · 演示进度' : '拖动进度，观察能量前沿' }}</p>
    </div>
    <div class="ops-energy-controls">
      <label><span>进度</span><input v-model.number="progress" type="range" min="0" max="100" step="1" aria-label="演示进度" :disabled="phase === 'loading'" @input="phase = 'idle'" /><output>{{ Math.round(progress) }}%</output></label>
      <label><span>速度</span><input v-model.number="speed" type="range" min="0.4" max="2" step="0.1" aria-label="动效速度" /><output>{{ speed.toFixed(1) }}×</output></label>
      <label><span>波幅</span><input v-model.number="amplitude" type="range" min="0" max="1.8" step="0.05" aria-label="前沿波幅" /><output>{{ amplitude.toFixed(2) }}</output></label>
    </div>
    <p class="ops-energy-note">实际请求没有可计算进度时，显示循环能量流与“连接中”，不显示模拟百分比。</p>
    <template #footer>
      <el-button :disabled="phase === 'loading'" @click="demonstrate(true)">演示异常反馈</el-button>
      <el-button type="primary" :loading="phase === 'loading'" @click="demonstrate()">{{ phase === 'error' ? '重新体验' : '体验加载与完成' }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.ops-feedback-intro { margin: 0; color: var(--ops-muted); font-size: 13px; line-height: 1.8; }
.ops-energy-demo { display: flex; flex-direction: column; align-items: center; padding: 30px 10px 15px; }
.ops-energy-state { min-height: 20px; margin: 18px 0 0; color: var(--ops-muted); font-size: 12px; }
.ops-energy-controls { display: flex; flex-direction: column; gap: 17px; margin: 12px 15px 25px; }
.ops-energy-controls label { display: grid; grid-template-columns: 40px minmax(0,1fr) 48px; align-items: center; gap: 14px; color: var(--ops-text-soft); font-size: 12px; }
.ops-energy-controls input { width: 100%; height: 16px; margin: 0; accent-color: var(--ops-brand); cursor: pointer; }
.ops-energy-controls output { text-align: right; font: 11px var(--ops-font-mono); font-variant-numeric: tabular-nums; }
.ops-energy-note { margin: 0 0 5px; padding: 12px 14px; border: 1px solid var(--ops-brand-line); border-radius: 8px; background: var(--ops-brand-soft); color: var(--ops-muted); font-size: 11px; line-height: 1.8; }
@media(max-width:560px) { .ops-energy-demo { padding-right: 0; padding-left: 0; } .ops-energy-controls { margin-right: 0; margin-left: 0; } }
</style>

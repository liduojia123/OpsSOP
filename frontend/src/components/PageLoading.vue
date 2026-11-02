<script setup lang="ts">
import EnergyProgress from './EnergyProgress.vue'
withDefaults(defineProps<{ label?: string; compact?: boolean }>(), { label: '正在加载', compact: false })
</script>

<template>
  <div class="ops-page-loading ops-context-energy" :class="{ 'is-compact': compact }" role="status" :aria-label="label" aria-live="polite">
    <div class="ops-page-loading-heading"><span>{{ label }}</span><span class="ops-loading-pulse" aria-hidden="true"></span></div>
    <EnergyProgress compact :label="label" />
    <div v-if="!compact" class="ops-loading-lines" aria-hidden="true"><i></i><i></i><i></i></div>
  </div>
</template>

<style scoped>
.ops-page-loading { width: min(100%, 440px); margin: 0 auto; padding: 44px 24px; }
.ops-page-loading.is-compact { padding: 24px; }
.ops-page-loading-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; color: var(--ops-text-soft); font-size: 13px; font-weight: 500; }
.ops-loading-pulse { width: 5px; height: 5px; border-radius: 50%; background: var(--ops-brand); box-shadow: 0 0 0 4px var(--ops-brand-soft); }
.ops-page-loading :deep(.ops-energy.is-compact) { height: 24px; border-radius: 6px; }
.ops-page-loading :deep(.ops-energy-copy) { display: none; }
.ops-loading-lines { display: grid; gap: 12px; margin-top: 28px; }
.ops-loading-lines i { height: 8px; border-radius: 2px; background: var(--ops-surface-warm); }
.ops-loading-lines i:nth-child(2) { width: 80%; }
.ops-loading-lines i:nth-child(3) { width: 55%; }
</style>

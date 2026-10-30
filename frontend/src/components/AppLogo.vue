<script setup lang="ts">
defineProps<{ dark?: boolean }>()
</script>

<template>
  <span class="ops-logo" :class="{ 'ops-logo-on-dark': dark }" role="img" aria-label="OpsSOP">
    <span class="ops-logo-art" aria-hidden="true"></span>
  </span>
</template>

<style scoped>
.ops-logo {
  position: relative;
  display: block;
  width: var(--ops-logo-width, 184px);
  max-width: 100%;
  aspect-ratio: 1632 / 528;
  flex-shrink: 0;
  overflow: hidden;
  --logo-ink: var(--ops-text, #232326);
  --logo-signal: var(--ops-brand, #c94b25);
}
.ops-logo-on-dark {
  --logo-ink: var(--ops-ink-on-dark, #f8f5eb);
  --logo-signal: var(--ops-brand-mark, #f16a3c);
}
.ops-logo-art {
  position: absolute;
  /* Preserve the approved artwork; crop its transparent margin in the layout. */
  width: 118.9951%;
  height: 153.2197%;
  left: -9.8039%;
  top: -24.2424%;
  background: linear-gradient(90deg, var(--logo-signal) 36.5%, var(--logo-ink) 36.5%);
  mask: url('../../assets/opssop-logo-v3.png') center / 100% 100% no-repeat;
}
</style>

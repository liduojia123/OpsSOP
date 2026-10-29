<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  book: 'M4 3h13a3 3 0 0 1 3 3v15H7a3 3 0 0 1-3-3V3z M4 17h16 M8 7h8 M8 11h6',
  files: 'M8 3h9l4 4v13H8z M17 3v5h4 M4 7H2v15h13',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
  'chevron-right': 'm9 5 7 7-7 7', 'chevron-left': 'm15 5-7 7 7 7',
  'arrow-right': 'M4 12h16 m-6-6 6 6-6 6', 'arrow-left': 'M20 12H4 m6-6-6 6 6 6',
  'arrow-up-right': 'M6 18 18 6 M6 6h12v12', plus: 'M12 5v14 M5 12h14',
  search: 'M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15 M16 16l5 5',
  check: 'm5 12 4 4L19 6', clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2',
  shield: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z m-4 9 3 3 5-6',
  logout: 'M9 4H4v16h5 M8 12h13 m-5-5 5 5-5 5',
  user: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M4 21v-2a8 8 0 0 1 16 0v2',
  bot: 'M5 7h14v13H5z M12 3v4 M2 11v5 M22 11v5 M9 12h.01 M15 12h.01 M9 16h6',
  link: 'm10 8 2-2a5 5 0 0 1 7 7l-3 3 M14 16l-2 2a5 5 0 0 1-7-7l3-3 M8 16l8-8',
  filter: 'M4 6h16 M7 12h10 M10 18h4',
  alert: 'm12 3 10 18H2z M12 9v5 M12 17h.01',
  'check-circle': 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 m-4 9 3 3 5-6',
  command: 'M8 8h8v8H8z M8 8H5a3 3 0 1 1 3-3v3 M16 8V5a3 3 0 1 1 3 3h-3 M16 16h3a3 3 0 1 1-3 3v-3 M8 16v3a3 3 0 1 1-3-3h3',
  lock: 'M5 10h14v11H5z M8 10V7a4 4 0 0 1 8 0v3 M12 14v3',
  server: 'M3 3h18v7H3z M3 14h18v7H3z M7 6h.01 M7 17h.01 M11 6h6 M11 17h6',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01', menu: 'M4 6h16 M4 12h16 M4 18h16',
  close: 'm6 6 12 12 M6 18 18 6', history: 'M3 11a9 9 0 1 1 1 6 M3 4v7h7 M12 7v5l4 2',
  refresh: 'M20 7a9 9 0 0 0-15-1L2 9 M2 3v6h6 M4 17a9 9 0 0 0 15 1l3-3 M22 21v-6h-6',
  edit: 'm14 5 5 5 M4 20l5-1L21 7l-5-5L4 14z',
  folder: 'M3 6h7l2 2h9v12H3z M3 6V4h7l2 2',
  workflow: 'M3 3h6v6H3z M15 15h6v6h-6z M15 3h6v6h-6z M9 6h6 M6 9v9h9',
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="ops-icon"><path :d="paths[name] || paths.files" /></svg>
</template>

<style scoped>.ops-icon { display: inline-block; flex-shrink: 0; vertical-align: middle; }</style>

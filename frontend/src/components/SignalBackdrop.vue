<script setup lang="ts">
// 装饰拓扑，不代表服务器或业务状态。
const traces = [
  'M-40 78H285L327 120H620L680 60H1080L1110 90H1640',
  'M-40 90H278L320 132H626L686 72H1073L1103 102H1640',
  'M410 -40V45L460 95H770L808 133H1260L1300 173H1640',
  'M430 -40V37L478 85H779L817 123H1268L1308 163H1640',
  'M-40 245H75L125 295V690L185 750V1040',
  'M-40 261H68L109 302V698L169 758V1040',
  'M1640 342H1510L1460 392V665L1390 735H1060L1015 780H540L480 840H-40',
  'M1640 358H1517L1476 399V673L1397 751H1067L1022 796H547L487 856H-40',
  'M1510 -40V215L1440 285V460L1385 515V1040',
  'M1526 -40V222L1456 292V467L1401 522V1040',
  'M260 1040V900L325 835H700L765 900H1140L1205 835H1640',
  'M276 1040V907L332 851H693L758 916H1147L1212 851H1640',
]
const junctions = [[327,120],[680,60],[808,133],[1300,173],[125,295],[185,750],[1460,392],[1390,735],[1015,780],[325,835],[765,900],[1440,285]]
</script>

<template>
  <div class="ops-signal-backdrop" aria-hidden="true">
    <svg viewBox="0 0 1600 1000" preserveAspectRatio="none" fill="none">
      <g class="ops-signal-wires"><path v-for="trace in traces" :key="trace" :d="trace" /></g>
      <g class="ops-signal-buses"><path d="M-20 184H1640M-20 610H1640M219 -20V1040M1332 -20V1040" /><path d="M46 170v28m-14-14h28M1332 170v28m-14-14h28M219 596v28m-14-14h28M1332 596v28m-14-14h28" /></g>
      <g class="ops-signal-streams">
        <path v-for="(trace, i) in traces.filter((_, i) => i % 2 === 0)" :key="trace" :d="trace" pathLength="1000" :style="{ '--trace-delay': `${i * -2.7}s`, '--trace-duration': `${12 + i * 2}s` }" />
      </g>
      <g v-for="([x,y], i) in junctions" :key="i" :transform="`translate(${x} ${y})`" class="ops-signal-junction" :style="{ '--node-delay': `${i * -.7}s` }"><circle class="ops-node-ring" r="11" /><path d="M-15 0h6m18 0h6M0-15v6m0 18v6" /><rect x="-3" y="-3" width="6" height="6" /></g>
      <g class="ops-signal-terminations"><path d="M14 32h60m-60 8h32M1540 32h46v46M14 930v46h46M1540 976h46v-46M768 32h64m-48 7h32" /></g>
    </svg>
    <div class="ops-signal-scan"></div>
  </div>
</template>

<style scoped>
.ops-signal-backdrop { position: absolute; inset: 0; overflow: hidden; pointer-events: none; background-image: linear-gradient(#899bb00b 1px, transparent 1px), linear-gradient(90deg, #899bb00b 1px, transparent 1px), radial-gradient(#8091a637 .7px, transparent .7px); background-size: 96px 96px, 96px 96px, 16px 16px; }
.ops-signal-backdrop svg { width: 100%; height: 100%; }
.ops-signal-backdrop svg path, .ops-signal-backdrop svg circle, .ops-signal-backdrop svg rect { vector-effect: non-scaling-stroke; }
.ops-signal-wires { stroke: #8b9bad; stroke-width: .8; opacity: .24; }
.ops-signal-buses { stroke: #8b9bad; stroke-width: .75; stroke-dasharray: 2 8; opacity: .23; }
.ops-signal-streams { stroke: var(--ops-brand); stroke-width: 1.5; opacity: .55; }
.ops-signal-streams path { stroke-dasharray: 4 8 36 952; animation: ops-signal-transit var(--trace-duration) linear infinite; animation-delay: var(--trace-delay); }
.ops-signal-junction { stroke: #a36b50; stroke-width: .8; opacity: .5; }
.ops-signal-junction rect { fill: var(--ops-canvas); stroke: var(--ops-brand); }
.ops-node-ring { transform-box: fill-box; transform-origin: center; animation: ops-node-pulse 5s ease-out infinite; animation-delay: var(--node-delay); }
.ops-signal-terminations { stroke: #899bae; stroke-width: 1.25; opacity: .45; }
.ops-signal-scan { position: absolute; inset: 0; background: linear-gradient(180deg, transparent 48%, #c94b2508 50%, transparent 52%); background-size: 100% 200%; animation: ops-field-scan 18s linear infinite; }
@keyframes ops-signal-transit { to { stroke-dashoffset: -1000; } }
@keyframes ops-node-pulse { 0%, 15% { opacity: .8; transform: scale(.55); } 70%, 100% { opacity: 0; transform: scale(1.6); } }
@keyframes ops-field-scan { from { background-position: 0 100%; } to { background-position: 0 -100%; } }
@media (prefers-reduced-motion: reduce) { .ops-signal-streams path, .ops-node-ring, .ops-signal-scan { animation: none; } .ops-signal-scan { display: none; } }
</style>

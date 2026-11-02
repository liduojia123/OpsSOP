<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import EnergyProgress from '../components/EnergyProgress.vue'
import client, { errMsg } from '../api'
import { caseStatusMap, faultStatusMap, fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Workbench } from '../types'

const auth = useAuthStore()
const wb = ref<Workbench | null>(null)
const loading = ref(true)
const loadError = ref('')

const metrics = computed(() => [
  { label: '我的待处理故障', value: wb.value?.my_open_faults.length ?? 0, icon: 'activity', path: '/faults', tone: 'default' },
  { label: '待审核案例', value: wb.value?.pending_case_reviews.length ?? 0, icon: 'files', path: '/cases', tone: 'warning' },
  { label: '待审核 SOP', value: wb.value?.pending_sop_reviews.length ?? 0, icon: 'clock', path: '/sops', tone: 'warning' },
  { label: '有效 SOP', value: wb.value?.published_sop_count ?? 0, icon: 'book', path: '/sops', tone: 'brand' },
])

const reviewCount = computed(() => (wb.value?.pending_case_reviews.length ?? 0) + (wb.value?.pending_sop_reviews.length ?? 0))
const returnedCount = computed(() => (wb.value?.my_returned_cases.length ?? 0) + (wb.value?.my_returned_sops.length ?? 0))
const maxCategoryCount = computed(() => Math.max(1, ...(wb.value?.category_stats.map((category) => category.count) ?? [])))

async function loadWorkbench() {
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await client.get('/api/workbench')
    wb.value = data
  } catch (e) {
    loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

onMounted(loadWorkbench)
</script>

<template>
  <div class="ops-page ops-dashboard">
    <div class="ops-page-header">
      <div>
        <h1>工作台</h1>
      </div>
      <router-link v-if="auth.role !== 'reviewer'" to="/faults" class="ops-dashboard-primary">
        <AppIcon name="activity" :size="16" /><span>处理故障</span><AppIcon name="arrow-right" :size="16" />
      </router-link>
      <router-link v-else to="/cases" class="ops-dashboard-primary">
        <AppIcon name="shield" :size="16" /><span>查看审核任务</span><AppIcon name="arrow-right" :size="16" />
      </router-link>
    </div>

    <nav class="ops-flow-strip" aria-label="知识工作流程">
      <router-link v-for="(step, i) in [{ title: '记录现场', note: '故障处理', path: '/faults', icon: 'activity' }, { title: '确认经验', note: '案例归档', path: '/cases', icon: 'files' }, { title: '沉淀标准', note: 'SOP 知识库', path: '/sops', icon: 'book' }, { title: '再次复用', note: '知识搜索', path: '/search', icon: 'search' }]" :key="step.path" :to="step.path">
        <span class="ops-flow-number">0{{ i + 1 }}</span><AppIcon :name="step.icon" :size="18" /><span><strong>{{ step.title }}</strong><small>{{ step.note }}</small></span><AppIcon name="arrow-right" :size="14" class="ops-flow-arrow" />
      </router-link>
    </nav>

    <div v-if="loading" class="ops-dashboard-loading" aria-label="正在加载工作台" role="status">
      <EnergyProgress class="ops-dashboard-energy" label="正在加载工作台" subtitle="正在读取故障与知识更新" />
      <div v-for="item in 4" :key="item" class="ops-metric-skeleton"><span></span><strong></strong><small></small></div>
    </div>
    <div v-else-if="loadError" class="ops-panel">
      <el-alert title="无法加载工作台" :description="loadError" type="error" :closable="false" show-icon />
      <el-button class="mt-4" @click="loadWorkbench">重新加载</el-button>
    </div>

    <template v-else-if="wb">
      <div class="ops-metric-grid">
        <router-link
          v-for="(metric, index) in metrics"
          :key="metric.label"
          :to="metric.path"
          class="ops-metric-card ops-reveal"
          :class="'is-' + metric.tone"
          :style="{ '--ops-order': index }"
        >
          <div class="ops-metric-top"><span>{{ metric.label }}</span><AppIcon :name="metric.icon" :size="18" /></div>
          <strong>{{ metric.value }}</strong>
          <AppIcon name="arrow-up-right" :size="16" class="ops-metric-arrow" />
        </router-link>
      </div>

      <div class="ops-dashboard-grid">
        <section class="ops-panel ops-dashboard-panel is-sops ops-reveal" style="--ops-order: 0; --ops-delay: 40ms">
          <div class="ops-section-header">
            <div class="ops-dashboard-section-title"><AppIcon name="book" :size="18" /><h2>最近发布的 SOP</h2></div>
            <router-link to="/sops" class="ops-panel-link">进入知识库<AppIcon name="arrow-up-right" :size="14" /></router-link>
          </div>
          <div v-if="wb.recent_sops.length === 0" class="ops-empty is-quiet">
            <AppIcon name="book" :size="22" />
            <div><strong>暂无已发布 SOP</strong><span>审核发布后，按时间出现在这里。</span></div>
          </div>
          <div v-else class="ops-sop-list">
            <router-link v-for="sop in wb.recent_sops" :key="sop.id" :to="'/sops/' + sop.id" class="ops-sop-row ops-row-hover">
              <span class="ops-document-icon"><AppIcon name="book" :size="20" /></span>
              <div class="ops-work-copy"><strong>{{ sop.title }}</strong><span>发布于 {{ fmtTime(sop.published_at) }}</span></div>
              <span class="ops-version-label">v{{ sop.version }}</span>
              <AppIcon name="chevron-right" :size="15" class="ops-row-arrow" />
            </router-link>
          </div>
        </section>

        <section class="ops-panel ops-dashboard-panel is-review ops-reveal" style="--ops-order: 1; --ops-delay: 40ms">
          <div class="ops-section-header">
            <div class="ops-dashboard-section-title"><AppIcon name="shield" :size="18" /><h2>待审核任务</h2><span class="ops-count">{{ reviewCount }}</span></div>
          </div>
          <div v-if="reviewCount === 0" class="ops-empty">
            <AppIcon name="check-circle" :size="22" />
            <div><strong>暂无待审核任务</strong><span>审核通过后进入正式知识。</span></div>
          </div>
          <div v-else class="ops-review-list">
            <router-link v-for="item in wb.pending_case_reviews" :key="'case-' + item.id" :to="'/cases/' + item.id" class="ops-review-row ops-row-hover">
              <span class="ops-review-kind"><AppIcon name="files" :size="15" />案例</span>
              <strong>{{ item.title }}</strong>
              <span class="ops-review-state">{{ caseStatusMap[item.status].label }}<AppIcon name="chevron-right" :size="14" /></span>
            </router-link>
            <router-link v-for="item in wb.pending_sop_reviews" :key="'sop-' + item.id" :to="'/sops/' + item.id" class="ops-review-row ops-row-hover">
              <span class="ops-review-kind"><AppIcon name="book" :size="15" />SOP</span>
              <strong>{{ item.title }}</strong>
              <span class="ops-review-state">{{ sopStatusMap[item.status].label }}<AppIcon name="chevron-right" :size="14" /></span>
            </router-link>
          </div>
        </section>

        <section class="ops-panel ops-dashboard-panel is-faults ops-reveal" style="--ops-order: 2; --ops-delay: 40ms">
          <div class="ops-section-header">
            <div class="ops-dashboard-section-title"><AppIcon name="activity" :size="18" /><h2>我的待处理故障</h2><span class="ops-count">{{ wb.my_open_faults.length }}</span></div>
            <router-link to="/faults" class="ops-panel-link">全部故障<AppIcon name="arrow-up-right" :size="14" /></router-link>
          </div>
          <div v-if="wb.my_open_faults.length === 0" class="ops-empty">
            <AppIcon name="check-circle" :size="22" />
            <div><strong>当前没有待处理故障</strong><span>新的排查，从完整记录现场开始。</span></div>
          </div>
          <div v-else class="ops-work-list">
            <router-link v-for="fault in wb.my_open_faults" :key="fault.id" :to="'/faults/' + fault.id" class="ops-work-row ops-row-hover">
              <span class="ops-work-symbol"><AppIcon name="activity" :size="17" /></span>
              <div class="ops-work-copy">
                <strong>{{ fault.title }}</strong>
                <span>{{ fault.service || '未填服务' }}<i>·</i>{{ fmtTime(fault.updated_at) }}</span>
              </div>
              <el-tag size="small" :type="(faultStatusMap[fault.status].type as any)">{{ faultStatusMap[fault.status].label }}</el-tag>
              <AppIcon name="chevron-right" :size="15" class="ops-row-arrow" />
            </router-link>
          </div>
        </section>

        <section class="ops-panel ops-dashboard-panel is-categories ops-reveal" style="--ops-order: 3; --ops-delay: 40ms">
          <div class="ops-section-header">
            <div class="ops-dashboard-section-title"><AppIcon name="folder" :size="18" /><h2>故障分类分布</h2></div>
            <span class="ops-panel-caption">按记录数量</span>
          </div>
          <div v-if="wb.category_stats.every((category) => category.count === 0)" class="ops-empty is-quiet">
            <AppIcon name="folder" :size="22" />
            <div><strong>暂无故障数据</strong><span>记录故障后，这里显示分类分布。</span></div>
          </div>
          <div v-else class="ops-category-list">
            <div v-for="category in wb.category_stats" :key="category.category_name" class="ops-category-row">
              <div class="ops-category-label"><span>{{ category.category_name }}</span><strong>{{ category.count }}</strong></div>
              <div class="ops-category-track"><span :style="{ width: (category.count / maxCategoryCount * 100) + '%' }"></span></div>
            </div>
          </div>
        </section>
      </div>

      <section v-if="returnedCount > 0" class="ops-panel ops-returned-panel ops-reveal" style="--ops-order: 4; --ops-delay: 40ms">
        <div class="ops-section-header">
          <div class="ops-dashboard-section-title"><AppIcon name="alert" :size="18" /><h2>被退回，待我修改</h2><span class="ops-count">{{ returnedCount }}</span></div>
        </div>
        <div class="ops-returned-list">
          <router-link v-for="item in wb.my_returned_cases" :key="'returned-case-' + item.id" :to="'/cases/' + item.id" class="ops-returned-row ops-row-hover">
            <span class="ops-review-kind">案例</span><div><strong>{{ item.title }}</strong><p>审核意见：{{ item.review_comment }}</p></div><AppIcon name="arrow-right" :size="16" class="ops-row-arrow" />
          </router-link>
          <router-link v-for="item in wb.my_returned_sops" :key="'returned-sop-' + item.id" :to="'/sops/' + item.id" class="ops-returned-row ops-row-hover">
            <span class="ops-review-kind">SOP</span><div><strong>{{ item.title }}</strong><p>审核意见：{{ item.review_comment }}</p></div><AppIcon name="arrow-right" :size="16" class="ops-row-arrow" />
          </router-link>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.ops-dashboard { max-width: 1550px; }
.ops-dashboard .ops-page-header { align-items: center; padding: 0 0 24px; margin-bottom: 24px; }
.ops-dashboard .ops-page-header h1 { font-size: var(--ops-text-title); letter-spacing: -1px; margin: 0; }
.ops-dashboard-primary { display: inline-flex; align-items: center; gap: 10px; padding: 12px 18px; border-radius: 8px; background: var(--ops-brand); color: white; text-decoration: none; font-size: 13px; transition: background .2s, transform .2s; }
.ops-dashboard-primary:hover { background: var(--ops-brand-hover); transform: translateY(-2px); }
.ops-flow-strip { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); margin-bottom: 25px; border: 1px solid var(--ops-border); border-radius: 12px; background: white; overflow: hidden; }
.ops-flow-strip > a { position: relative; display: flex; align-items: center; gap: 13px; min-width: 0; padding: 19px 22px; color: var(--ops-text-soft); text-decoration: none; border-right: 1px solid var(--ops-border-light); transition: background .2s; }
.ops-flow-strip > a:last-child { border-right: 0; }
.ops-flow-strip > a::before { content: ''; position: absolute; bottom: 7px; left: 22px; right: 22px; height: 1px; background: linear-gradient(90deg, transparent 0 35%, var(--ops-brand) 45% 53%, transparent 65%); background-size: 300% 100%; opacity: .4; animation: ops-flow-packet 6s linear infinite; }
.ops-flow-strip > a:nth-child(2)::before { animation-delay: -4.5s; }
.ops-flow-strip > a:nth-child(3)::before { animation-delay: -3s; }
.ops-flow-strip > a:nth-child(4)::before { animation-delay: -1.5s; }
@keyframes ops-flow-packet { from { background-position: 150% 0; } to { background-position: -150% 0; } }
@media (prefers-reduced-motion: reduce) { .ops-flow-strip > a::before { animation: none; } }
.ops-flow-strip > a::after { content: ''; position: absolute; bottom: 0; left: 0; width: 100%; height: 2px; background: var(--ops-brand); transform: scaleX(0); transform-origin: left; transition: transform .3s; }
.ops-flow-strip > a:hover { background: var(--ops-surface-muted); }
.ops-flow-strip > a:hover::after { transform: scaleX(1); }
.ops-flow-number { align-self: flex-start; font: 10px var(--ops-font-mono); color: var(--ops-faint); }
.ops-flow-strip strong, .ops-flow-strip small { display: block; }
.ops-flow-strip strong { font-size: 12px; font-weight: 600; }
.ops-flow-strip small { margin-top: 5px; font-size: 10px; color: var(--ops-muted); }
.ops-flow-arrow { margin-left: auto; color: var(--ops-faint); }
.ops-metric-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 0; margin-bottom: 24px; overflow: hidden; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); background: var(--ops-surface); }
.ops-metric-card { position: relative; min-width: 0; padding: 24px; border-right: 1px solid var(--ops-border-light); color: var(--ops-text); text-decoration: none; transition: background var(--ops-duration-fast); }
.ops-metric-card:last-child { border-right: 0; }
.ops-metric-arrow { position: absolute; right: 24px; bottom: 28px; color: var(--ops-faint); transition: color .2s, transform .2s; }
.ops-metric-card:hover .ops-metric-arrow { color: var(--ops-brand); transform: translate(2px, -2px); }
.ops-metric-card:hover { background: var(--ops-brand-soft); }
.ops-metric-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--ops-metric-label); font-size: 12px; }
.ops-metric-top > svg { color: var(--ops-faint); }
.ops-metric-card > strong { display: block; margin: 24px 0 0; font: 500 42px/1 var(--ops-font-mono); letter-spacing: -2px; color: var(--ops-text); }
.ops-metric-card.is-brand { background: var(--ops-brand-soft); }
.ops-metric-card.is-brand > strong { color: var(--ops-brand); }
.ops-dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); grid-template-areas: "faults review" "sops categories"; gap: 22px; }
.is-sops { grid-area: sops; }
.is-sops .ops-section-header { border-bottom-color: var(--ops-border); background: var(--ops-paper-band); }
.is-review { grid-area: review; }
.is-faults { grid-area: faults; }
.is-categories { grid-area: categories; }
.ops-dashboard-panel { min-width: 0; padding: 0; }
.ops-dashboard-panel .ops-section-header { min-height: 54px; gap: 16px; padding: 14px 20px; }
.ops-dashboard-section-title { display: flex; align-items: center; gap: 9px; min-width: 0; }
.ops-dashboard-section-title > svg { flex-shrink: 0; color: var(--ops-brand); }
.ops-dashboard-section-title > h2 { margin: 0; color: var(--ops-text); font-size: 14px; font-weight: 600; white-space: nowrap; }
.ops-count { display: inline-grid; min-width: 20px; height: 20px; padding: 0 5px; place-items: center; border-radius: 4px; background: var(--ops-surface-warm); color: var(--ops-muted); font-family: var(--ops-font-mono); font-size: 10px; font-weight: 500; font-variant-numeric: tabular-nums; }
.ops-panel-link { display: inline-flex; align-items: center; gap: 4px; color: var(--ops-muted); font-size: 11px; text-decoration: none; white-space: nowrap; transition: color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-panel-link:hover { color: var(--ops-brand); }
.ops-panel-caption { color: var(--ops-faint); font-size: 10px; white-space: nowrap; }

.ops-sop-list { max-height: 470px; overflow-y: auto; counter-reset: knowledge; }
.ops-sop-row { display: flex; align-items: center; gap: 14px; min-height: 90px; padding: 15px 22px; border-bottom: 1px solid var(--ops-border-light); color: var(--ops-text); text-decoration: none; }
.ops-sop-row::before { content: counter(knowledge, decimal-leading-zero); counter-increment: knowledge; flex-shrink: 0; color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 12px; }
.ops-sop-row:last-child, .ops-work-row:last-child, .ops-review-row:last-child { border-bottom: 0; }
.ops-document-icon { display: grid; place-items: center; width: 24px; flex-shrink: 0; color: var(--ops-brand); }
.ops-work-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 7px; }
.ops-work-copy > strong { overflow: hidden; color: var(--ops-text); font-size: 13px; font-weight: 550; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.ops-work-copy > span { overflow: hidden; color: var(--ops-muted); font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.6; text-overflow: ellipsis; white-space: nowrap; }
.ops-work-copy i { margin: 0 7px; font-style: normal; }
.ops-version-label { flex-shrink: 0; padding: 3px 6px; border: 1px solid var(--ops-border); border-radius: 3px; background: var(--ops-surface-muted); color: var(--ops-muted); font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.4; }
.ops-row-arrow { flex-shrink: 0; color: var(--ops-faint); }

.ops-review-list { max-height: 330px; overflow-y: auto; }
.ops-review-row { display: grid; grid-template-columns: 54px minmax(0, 1fr); align-items: center; gap: 8px 10px; min-height: 76px; padding: 14px 20px; border-bottom: 1px solid var(--ops-border-light); text-decoration: none; }
.ops-review-row > strong { overflow: hidden; color: var(--ops-text); font-size: 12px; font-weight: 550; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.ops-review-kind { display: inline-flex; align-items: center; gap: 5px; width: max-content; color: var(--ops-muted); font-size: 10px; }
.ops-review-state { display: flex; grid-column: 2; align-items: center; justify-content: space-between; color: var(--ops-warning); font-size: 10px; }
.ops-review-state > svg { color: var(--ops-faint); }

.ops-work-list { max-height: 330px; overflow-y: auto; }
.ops-work-row { display: flex; align-items: center; gap: 12px; min-height: 76px; padding: 14px 20px; border-bottom: 1px solid var(--ops-border-light); color: var(--ops-text); text-decoration: none; }
.ops-work-symbol { display: grid; place-items: center; width: 30px; height: 30px; flex-shrink: 0; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }

.ops-category-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; max-height: 330px; padding: 20px 22px; overflow-y: auto; }
.ops-category-label { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 8px; color: var(--ops-metric-label); font-size: 11px; }
.ops-category-label > strong { color: var(--ops-metric-label); font-family: var(--ops-font-mono); font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }
.ops-category-track { height: 3px; overflow: hidden; border-radius: 0; background: var(--ops-border-light); }
.ops-category-track > span { display: block; height: 100%; background: var(--ops-brand); transition: width var(--ops-duration-panel) var(--ops-ease-out); }

.ops-returned-panel { margin-top: 22px; padding: 0; border-color: var(--ops-warm-line); }
.ops-returned-panel .ops-dashboard-section-title > svg { color: var(--ops-warning); }
.ops-returned-row { display: flex; align-items: flex-start; gap: 14px; padding: 15px 20px; border-bottom: 1px solid var(--ops-warm-line-soft); color: var(--ops-text); text-decoration: none; }
.ops-returned-row:last-child { border-bottom: 0; }
.ops-returned-row > div { flex: 1; min-width: 0; }
.ops-returned-row strong { font-size: 12px; font-weight: 550; }
.ops-returned-row p { margin: 5px 0 0; color: var(--ops-muted); font-size: 11px; line-height: 1.6; }

.ops-dashboard-loading { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
.ops-dashboard-energy { grid-column: 1 / -1; max-width: 560px; margin: 0 auto 8px; }
.ops-metric-skeleton { display: flex; flex-direction: column; gap: 16px; min-height: 153px; padding: 22px; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); background: var(--ops-surface); }
.ops-metric-skeleton > span, .ops-metric-skeleton > strong, .ops-metric-skeleton > small { display: block; width: 65%; height: 11px; border-radius: 3px; background: var(--ops-surface-warm); }
.ops-metric-skeleton > strong { width: 35%; height: 30px; }
.ops-metric-skeleton > small { width: 48%; height: 8px; }


@media (max-width: 1200px) { .ops-flow-strip > a { padding: 16px 13px; gap: 9px; } .ops-flow-number { display: none; } .ops-panel-caption { display: none; } }
@media (max-width: 1050px) { .ops-metric-grid, .ops-dashboard-loading { grid-template-columns: repeat(2,minmax(0,1fr)); } .ops-dashboard-grid { grid-template-columns: minmax(0,1fr); grid-template-areas: "faults" "review" "sops" "categories"; } }
@media (max-width: 600px) { .ops-dashboard .ops-page-header { align-items: flex-start; flex-direction: column; gap: 18px; } .ops-dashboard .ops-page-header h1 { font-size: 32px; } .ops-flow-strip { grid-template-columns: repeat(2,minmax(0,1fr)); } .ops-flow-strip > a { border-bottom: 1px solid var(--ops-border-light); } .ops-metric-grid { gap: 0; } .ops-metric-card:nth-child(-n+2) { border-bottom: 1px solid var(--ops-border-light); } .ops-metric-card { padding: 18px 14px; } .ops-metric-top { font-size: 11px; } .ops-metric-card > strong { font-size: 32px; } .ops-section-header { flex-wrap: wrap; } .ops-work-row { gap: 8px; padding: 14px; } .ops-work-symbol, .ops-document-icon { display: none; } .ops-sop-row { padding: 14px; gap: 10px; } }
</style>

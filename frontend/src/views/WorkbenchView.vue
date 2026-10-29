<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from '../components/AppIcon.vue'
import client, { errMsg } from '../api'
import { caseStatusMap, faultStatusMap, fmtTime, sopStatusMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { Workbench } from '../types'

const auth = useAuthStore()
const wb = ref<Workbench | null>(null)
const loading = ref(true)
const loadError = ref('')

const metrics = computed(() => [
  { label: '我的待处理故障', value: wb.value?.my_open_faults.length ?? 0, icon: 'activity', note: '需要继续跟进', tone: 'default' },
  { label: '待审核案例', value: wb.value?.pending_case_reviews.length ?? 0, icon: 'files', note: '等待知识审核', tone: 'warning' },
  { label: '待审核 SOP', value: wb.value?.pending_sop_reviews.length ?? 0, icon: 'clock', note: '等待发布确认', tone: 'warning' },
  { label: '有效 SOP', value: wb.value?.published_sop_count ?? 0, icon: 'book', note: '当前有效版本', tone: 'brand' },
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
        <div class="ops-eyebrow">OPSSOP / OPERATIONS MEMORY</div>
        <h1 aria-label="工作台">故障有解。<br />经验有迹<span class="signal-cursor">_</span></h1>
        <p class="ops-page-description">记录现场，确认根因，让一次解决成为下一次的起点。</p>
      </div>
      <div class="signal-art" aria-hidden="true">
        <span class="signal-art-label">THE KNOWLEDGE LOOP</span>
        <svg viewBox="0 0 300 150" fill="none"><ellipse cx="150" cy="75" rx="125" ry="38" transform="rotate(-25 150 75)"/><ellipse cx="150" cy="75" rx="125" ry="38" transform="rotate(25 150 75)"/><path d="M25 75h250M150 15v120" stroke-dasharray="2 7"/><circle cx="150" cy="75" r="23"/><circle cx="40" cy="103" r="5" class="signal-node"/><circle cx="258" cy="46" r="5" class="signal-node"/></svg>
        <span class="signal-art-caption">OBSERVE / RESOLVE / REMEMBER</span>
      </div>
      <router-link v-if="auth.role !== 'reviewer'" to="/faults" class="ops-dashboard-primary">
        <AppIcon name="activity" :size="16" /><span>处理故障</span><AppIcon name="arrow-right" :size="16" />
      </router-link>
      <router-link v-else to="/cases" class="ops-dashboard-primary">
        <AppIcon name="shield" :size="16" /><span>查看审核任务</span><AppIcon name="arrow-right" :size="16" />
      </router-link>
    </div>

    <div v-if="loading" class="ops-dashboard-loading" aria-label="正在加载工作台" role="status">
      <div v-for="item in 4" :key="item" class="ops-metric-skeleton"><span></span><strong></strong><small></small></div>
    </div>
    <div v-else-if="loadError" class="ops-panel">
      <el-alert title="无法加载工作台" :description="loadError" type="error" :closable="false" show-icon />
      <el-button class="mt-4" @click="loadWorkbench">重新加载</el-button>
    </div>

    <template v-else-if="wb">
      <div class="ops-metric-grid">
        <div
          v-for="(metric, index) in metrics"
          :key="metric.label"
          class="ops-metric-card ops-reveal"
          :class="'is-' + metric.tone"
          :style="{ '--ops-order': index }"
        >
          <div class="ops-metric-top"><span>{{ metric.label }}</span><AppIcon :name="metric.icon" :size="18" /></div>
          <strong>{{ metric.value }}</strong>
          <div class="ops-metric-note"><span class="ops-metric-dot"></span>{{ metric.note }}</div>
        </div>
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
            <span class="ops-panel-caption">确认后成为团队知识</span>
          </div>
          <div v-if="reviewCount === 0" class="ops-empty">
            <AppIcon name="check-circle" :size="22" />
            <div><strong>暂无待审核任务</strong><span>审核通过后进入正式知识。</span></div>
          </div>
          <div v-else class="ops-review-list">
            <router-link v-for="item in wb.pending_case_reviews" :key="'case-' + item.id" to="/cases" class="ops-review-row ops-row-hover">
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
          <router-link v-for="item in wb.my_returned_cases" :key="'returned-case-' + item.id" to="/cases" class="ops-returned-row ops-row-hover">
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
.ops-dashboard { max-width: 1600px; margin: 0 auto; }
.ops-dashboard .ops-page-header { display: block; position: relative; min-height: 245px; padding: 30px 34px; margin-bottom: 0; overflow: hidden; border-radius: var(--ops-radius-panel) var(--ops-radius-panel) 0 0; background: var(--ops-hero); color: #f5f0e3; }
.ops-dashboard .ops-page-header::after { content: ''; position: absolute; top: 0; right: 0; width: 11px; height: 100%; background: var(--ops-signal-stripe); }
.ops-dashboard .ops-page-header .ops-eyebrow { color: #b3aba0; font-size: 10px; letter-spacing: 2.4px; }
.ops-dashboard .ops-page-header h1 { max-width: 620px; margin: 16px 0 12px; color: #fbf7ed; font-size: 44px; font-weight: 700; line-height: 1.17; letter-spacing: -2px; }
.ops-dashboard .ops-page-header .ops-page-description { max-width: 520px; color: #aaa8a1; font-size: 12px; line-height: 1.8; }
.signal-cursor { color: var(--ops-brand-mark); }
.signal-art { position: absolute; top: 25px; right: 44px; width: 270px; pointer-events: none; }
.signal-art svg { width: 270px; height: 135px; stroke: #686865; stroke-width: 1; }
.signal-art svg circle:not(.signal-node) { fill: var(--ops-brand-mark); stroke: var(--ops-brand-mark); }
.signal-art .signal-node { fill: #f4e7c7; stroke: #f4e7c7; animation: ops-node-drift 11s var(--ops-ease-standard) infinite alternate; }
.signal-art .signal-node:nth-of-type(2) { animation-delay: -4.5s; }
@keyframes ops-node-drift { from { transform: translate(0, 0); } to { transform: translate(2px, -2px); } }
.signal-art-label { display: block; color: #d3c5a9; font-family: var(--ops-font-mono); font-size: 9px; letter-spacing: 2.5px; text-align: center; }
.signal-art-caption { display: block; margin-top: -8px; color: #94938e; font-family: var(--ops-font-mono); font-size: 8px; letter-spacing: 1.2px; text-align: center; }
.ops-dashboard-primary { position: absolute; right: 44px; bottom: 25px; display: inline-flex; align-items: center; gap: 9px; min-height: 34px; padding: 0 14px; border: 1px solid #f0e9d8; border-radius: 3px; background: #f0e9d8; color: #27282b; font-size: 12px; text-decoration: none; transition: background-color var(--ops-duration-fast) var(--ops-ease-standard), border-color var(--ops-duration-fast) var(--ops-ease-standard), color var(--ops-duration-fast) var(--ops-ease-standard); }
.ops-dashboard-primary:hover { border-color: #fff9eb; background: #fff9eb; color: var(--ops-brand); }
.ops-dashboard-primary > svg:last-child { transition: transform var(--ops-duration-base) var(--ops-ease-out); }
.ops-dashboard-primary:hover > svg:last-child { transform: translateX(3px); }

.ops-metric-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; margin-bottom: 26px; overflow: hidden; border: 1px solid #d7d2c5; border-top: 0; border-radius: 0 0 var(--ops-radius-panel) var(--ops-radius-panel); }
.ops-metric-card { padding: 20px 24px 17px; border-right: 1px solid var(--ops-border); background: var(--ops-surface); }
.ops-metric-card:last-child { border-right: 0; }
.ops-metric-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--ops-metric-label); font-size: 11px; }
.ops-metric-top > svg { color: #8b8578; }
.ops-metric-card > strong { display: block; margin: 15px 0 8px; color: var(--ops-metric-ink); font-family: var(--ops-font-mono); font-size: 54px; font-weight: 500; line-height: 1; letter-spacing: -4px; font-variant-numeric: tabular-nums; }
.ops-metric-note { display: flex; align-items: center; gap: 6px; color: var(--ops-metric-note); font-size: 9px; line-height: 1.6; }
.ops-metric-dot { width: 4px; height: 4px; border-radius: 50%; background: #b3a98f; }
.ops-metric-card.is-warning .ops-metric-dot { background: #c59a4d; }
.ops-metric-card.is-brand { background: #ede9df; }
.ops-metric-card.is-brand > strong { color: var(--ops-brand-ink); }
.ops-metric-card.is-brand .ops-metric-dot { background: var(--ops-brand-ink); }

.ops-dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); grid-template-areas: "sops review" "sops faults" "categories categories"; gap: 22px; }
.is-sops { grid-area: sops; }
.is-sops .ops-section-header { border-bottom-color: #ded7c9; background: var(--ops-paper-band); }
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
.ops-sop-row::before { content: counter(knowledge, decimal-leading-zero); counter-increment: knowledge; flex-shrink: 0; color: #b4a78f; font-family: var(--ops-font-mono); font-size: 12px; }
.ops-sop-row:last-child, .ops-work-row:last-child, .ops-review-row:last-child { border-bottom: 0; }
.ops-document-icon { display: grid; place-items: center; width: 24px; flex-shrink: 0; color: #a36344; }
.ops-work-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 7px; }
.ops-work-copy > strong { overflow: hidden; color: var(--ops-text); font-size: 13px; font-weight: 550; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.ops-work-copy > span { overflow: hidden; color: #9a9283; font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.6; text-overflow: ellipsis; white-space: nowrap; }
.ops-work-copy i { margin: 0 7px; font-style: normal; }
.ops-version-label { flex-shrink: 0; padding: 3px 6px; border: 1px solid #ded7c9; border-radius: 3px; background: #efebe1; color: #81735d; font-family: var(--ops-font-mono); font-size: 10px; line-height: 1.4; }
.ops-row-arrow { flex-shrink: 0; color: var(--ops-faint); }

.ops-review-list { max-height: 330px; overflow-y: auto; }
.ops-review-row { display: grid; grid-template-columns: 54px minmax(0, 1fr); align-items: center; gap: 8px 10px; min-height: 76px; padding: 14px 20px; border-bottom: 1px solid var(--ops-border-light); text-decoration: none; }
.ops-review-row > strong { overflow: hidden; color: var(--ops-text); font-size: 12px; font-weight: 550; line-height: 1.5; text-overflow: ellipsis; white-space: nowrap; }
.ops-review-kind { display: inline-flex; align-items: center; gap: 5px; width: max-content; color: #99846b; font-size: 10px; }
.ops-review-state { display: flex; grid-column: 2; align-items: center; justify-content: space-between; color: var(--ops-warning); font-size: 10px; }
.ops-review-state > svg { color: var(--ops-faint); }

.ops-work-list { max-height: 330px; overflow-y: auto; }
.ops-work-row { display: flex; align-items: center; gap: 12px; min-height: 76px; padding: 14px 20px; border-bottom: 1px solid var(--ops-border-light); color: var(--ops-text); text-decoration: none; }
.ops-work-symbol { display: grid; place-items: center; width: 30px; height: 30px; flex-shrink: 0; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }

.ops-category-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 22px; max-height: 330px; padding: 20px 22px; overflow-y: auto; }
.ops-category-label { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 8px; color: var(--ops-metric-label); font-size: 11px; }
.ops-category-label > strong { color: var(--ops-metric-label); font-family: var(--ops-font-mono); font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }
.ops-category-track { height: 3px; overflow: hidden; border-radius: 0; background: #e5e0d6; }
.ops-category-track > span { display: block; height: 100%; background: #c76540; transition: width var(--ops-duration-panel) var(--ops-ease-out); }

.ops-returned-panel { margin-top: 22px; padding: 0; border-color: var(--ops-warm-line); }
.ops-returned-panel .ops-dashboard-section-title > svg { color: var(--ops-warning); }
.ops-returned-row { display: flex; align-items: flex-start; gap: 14px; padding: 15px 20px; border-bottom: 1px solid var(--ops-warm-line-soft); color: var(--ops-text); text-decoration: none; }
.ops-returned-row:last-child { border-bottom: 0; }
.ops-returned-row > div { flex: 1; min-width: 0; }
.ops-returned-row strong { font-size: 12px; font-weight: 550; }
.ops-returned-row p { margin: 5px 0 0; color: #968365; font-size: 11px; line-height: 1.6; }

.ops-dashboard-loading { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
.ops-metric-skeleton { display: flex; flex-direction: column; gap: 16px; min-height: 153px; padding: 22px; border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); background: var(--ops-surface); }
.ops-metric-skeleton > span, .ops-metric-skeleton > strong, .ops-metric-skeleton > small { display: block; width: 65%; height: 11px; border-radius: 3px; background: var(--ops-subtle); }
.ops-metric-skeleton > strong { width: 35%; height: 30px; }
.ops-metric-skeleton > small { width: 48%; height: 8px; }

@media (max-width: 1200px) {
  .ops-dashboard .ops-page-header h1 { font-size: 40px; }
  .ops-metric-card { padding: 18px; }
  .ops-metric-card > strong { font-size: 46px; letter-spacing: -3px; }
  .ops-dashboard-grid { gap: 18px; }
  .ops-panel-caption { display: none; }
  .ops-category-list { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
}
@media (max-width: 1050px) {
  .ops-metric-grid, .ops-dashboard-loading { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; overflow: visible; border: 0; }
  .ops-metric-card { border: 1px solid var(--ops-border); border-radius: var(--ops-radius-panel); }
  .ops-metric-grid { border: 0; }
  .ops-metric-skeleton { min-height: 132px; }
  .ops-dashboard-grid { grid-template-columns: minmax(0, 1fr); grid-template-areas: "sops" "review" "faults" "categories"; gap: 18px; }
  .ops-category-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .signal-art { display: none; }
  .ops-dashboard .ops-page-header { min-height: 275px; padding: 25px; }
  .ops-dashboard .ops-page-header h1 { font-size: 36px; letter-spacing: -1.6px; }
  .ops-dashboard-primary { right: 25px; bottom: 20px; }
  .ops-dashboard-panel .ops-section-header { padding: 14px 16px; gap: 10px; }
  .ops-sop-row, .ops-work-row, .ops-review-row, .ops-returned-row { padding: 13px 16px; }
  .ops-sop-row { min-height: 82px; gap: 10px; }
  .ops-sop-row::before { display: none; }
  .ops-document-icon { display: none; }
}
@media (max-width: 560px) {
  .ops-dashboard .ops-page-header { padding: 22px 20px; }
  .ops-dashboard .ops-page-header h1 { font-size: 31px; }
  .ops-metric-card { padding: 16px; }
  .ops-metric-top { align-items: flex-start; font-size: 11px; }
  .ops-metric-top > svg { display: none; }
  .ops-metric-card > strong { font-size: 42px; }
  .ops-metric-note { font-size: 9px; }
  .ops-work-symbol, .ops-row-arrow { display: none; }
  .ops-work-copy > strong { white-space: normal; }
  .ops-category-list { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; padding: 16px; }
}
</style>





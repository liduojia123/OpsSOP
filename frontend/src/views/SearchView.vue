<script setup lang="ts">
/**
 * 统一检索:一次搜索故障、案例与 SOP。
 * 结果范围由后端按当前账号权限裁剪;此页只负责输入、筛选与跳转,不自行拼接详情。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { caseStatusMap, faultStatusMap, fmtTime, sopStatusMap } from '../labels'
import type {
  CaseStatus,
  FaultStatus,
  SopStatus,
  UnifiedHit,
  UnifiedSearchResult,
} from '../types'

const TYPE_OPTIONS = [
  { value: 'fault', label: '故障', icon: 'activity' },
  { value: 'case', label: '案例', icon: 'files' },
  { value: 'sop', label: 'SOP', icon: 'book' },
] as const
const EXAMPLES = ['504', '磁盘', 'order-service', 'OOM']
const LIMIT = 8

const route = useRoute()
const router = useRouter()
const query = ref('')
const types = ref<string[]>(['fault', 'case', 'sop'])
const loading = ref(false)
const error = ref('')
const result = ref<UnifiedSearchResult | null>(null)
const searched = ref(false)

const groups = computed(() => (result.value?.groups ?? []).filter((group) => group.items.length))
const totalCount = computed(() =>
  (result.value?.groups ?? []).reduce((sum, group) => sum + group.total, 0),
)

function typeLabel(type: string): string {
  return TYPE_OPTIONS.find((item) => item.value === type)?.label ?? type
}

function typeIcon(type: string): string {
  return TYPE_OPTIONS.find((item) => item.value === type)?.icon ?? 'files'
}

function statusMeta(hit: UnifiedHit): { label: string; type: string } {
  const fallback = { label: hit.status, type: 'info' }
  if (hit.type === 'fault') {
    return faultStatusMap[hit.status as FaultStatus] ?? fallback
  }
  if (hit.type === 'case') {
    return caseStatusMap[hit.status as CaseStatus] ?? fallback
  }
  return sopStatusMap[hit.status as SopStatus] ?? fallback
}

function syncQuery() {
  const next: Record<string, string> = {}
  if (query.value.trim()) next.q = query.value.trim()
  if (types.value.length > 0 && types.value.length < TYPE_OPTIONS.length) {
    next.types = types.value.join(',')
  }
  void router.replace({ path: '/search', query: next })
}

async function search() {
  const q = query.value.trim()
  if (!q) {
    result.value = null
    searched.value = false
    syncQuery()
    return
  }
  loading.value = true
  error.value = ''
  searched.value = true
  syncQuery()
  try {
    const { data } = await client.get<UnifiedSearchResult>('/api/search/all', {
      params: { q, types: types.value.join(','), limit: LIMIT },
    })
    result.value = data
  } catch (e) {
    error.value = errMsg(e)
    result.value = null
  } finally {
    loading.value = false
  }
}

function toggleType(value: string) {
  if (types.value.includes(value)) {
    if (types.value.length === 1) return
    types.value = types.value.filter((item) => item !== value)
  } else {
    types.value = [...types.value, value]
  }
  if (query.value.trim()) void search()
}

function runExample(text: string) {
  query.value = text
  void search()
}

onMounted(() => {
  const initialQuery = typeof route.query.q === 'string' ? route.query.q : ''
  const rawTypes = typeof route.query.types === 'string' ? route.query.types.split(',') : []
  const validTypes = rawTypes.filter((item) =>
    TYPE_OPTIONS.some((option) => option.value === item),
  )
  if (validTypes.length) types.value = validTypes
  if (initialQuery) {
    query.value = initialQuery
    void search()
  }
})
</script>

<template>
  <div class="ops-page search-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">WORKSPACE / UNIFIED SEARCH</div>
        <h1>搜索</h1>
        <p class="ops-page-description">
          一次搜索故障、案例与 SOP；结果范围与当前账号权限一致，可直接打开对应记录。
        </p>
      </div>
    </header>

    <section class="ops-panel search-bar-panel">
      <div class="search-input-row">
        <el-input
          v-model="query"
          size="large"
          clearable
          placeholder="输入关键词：错误码、服务名、标题、症状…"
          @keyup.enter="search"
          @clear="search"
        />
        <el-button type="primary" size="large" :loading="loading" @click="search">搜索</el-button>
      </div>
      <div class="search-types" role="group" aria-label="结果类型">
        <button
          v-for="option in TYPE_OPTIONS"
          :key="option.value"
          type="button"
          class="search-type"
          :class="{ 'is-active': types.includes(option.value) }"
          @click="toggleType(option.value)"
        >
          <AppIcon :name="option.icon" :size="14" />{{ option.label }}
        </button>
        <span class="ops-section-caption">至少保留一种类型</span>
      </div>
    </section>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="mb-4" />

    <div v-if="!searched" class="ops-panel search-intro">
      <span class="ops-icon-tile"><AppIcon name="search" :size="20" /></span>
      <h3>从一个关键词开始</h3>
      <p>结果按故障、案例、SOP 分组展示；只搜索标题、描述/症状与服务等摘要字段。</p>
      <div class="search-examples">
        <button
          v-for="text in EXAMPLES"
          :key="text"
          type="button"
          class="ops-chip is-compact"
          @click="runExample(text)"
        >
          {{ text }}
        </button>
      </div>
    </div>

    <div v-else-if="loading && !result" class="ops-panel search-state">搜索中…</div>

    <div v-else-if="!groups.length" class="ops-panel search-state">
      <AppIcon name="search" :size="20" />
      <div>
        <strong>没有匹配结果</strong>
        <span>可尝试更短的关键词，或调整上方的类型筛选。</span>
      </div>
    </div>

    <template v-else>
      <div class="search-summary">
        共 {{ totalCount }} 条结果<span v-if="result?.query"> · 关键词「{{ result.query }}」</span>
      </div>
      <section v-for="group in groups" :key="group.type" class="ops-panel search-group">
        <div class="ops-section-header">
          <div class="search-group-title">
            <AppIcon :name="typeIcon(group.type)" :size="17" />
            <h2>{{ typeLabel(group.type) }}</h2>
            <span class="ops-count">{{ group.total }}</span>
          </div>
          <span v-if="group.truncated" class="ops-panel-caption">
            仅显示前 {{ group.items.length }} 条
          </span>
        </div>
        <div class="search-rows">
          <router-link
            v-for="hit in group.items"
            :key="hit.type + '-' + hit.id"
            :to="hit.link"
            class="search-row"
          >
            <span class="search-row-main">
              <strong>{{ hit.title }}</strong>
              <span class="search-row-sub">{{ hit.subtitle || '未填写摘要' }}</span>
            </span>
            <el-tag size="small" :type="(statusMeta(hit).type as any)">
              {{ statusMeta(hit).label }}
            </el-tag>
            <span class="search-row-time">{{ fmtTime(hit.updated_at) }}</span>
            <AppIcon name="chevron-right" :size="15" class="search-row-arrow" />
          </router-link>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.search-page {
  max-width: 1180px;
}
.search-bar-panel {
  padding: 18px 20px;
  margin-bottom: 18px;
}
.search-input-row {
  display: flex;
  gap: 10px;
}
.search-input-row :deep(.el-input) {
  flex: 1;
}
.search-types {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.search-type {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border: 1px solid var(--ops-border);
  border-radius: 999px;
  background: var(--ops-surface);
  color: var(--ops-text-soft);
  font-size: 12px;
  cursor: pointer;
}
.search-type.is-active {
  border-color: var(--ops-brand-line);
  background: var(--ops-brand-soft);
  color: var(--ops-brand);
}
.search-intro {
  padding: 28px;
}
.search-intro h3 {
  margin: 12px 0 6px;
  color: var(--ops-text);
  font-size: 15px;
}
.search-intro p {
  margin: 0 0 14px;
  color: var(--ops-muted);
  font-size: 12px;
  line-height: 1.7;
}
.search-examples {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.search-state {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 28px;
  color: var(--ops-muted);
  font-size: 12px;
  line-height: 1.7;
}
.search-state strong {
  display: block;
  color: var(--ops-text);
  font-size: 13px;
}
.search-summary {
  margin: 0 0 12px;
  color: var(--ops-muted);
  font-size: 11px;
}
.search-group {
  margin-bottom: 14px;
}
.search-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.search-group-title h2 {
  margin: 0;
  color: var(--ops-text);
  font-size: 14px;
}
.search-rows {
  display: flex;
  flex-direction: column;
}
.search-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto 18px;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-top: 1px solid var(--ops-border-light);
  color: inherit;
  text-decoration: none;
}
.search-row:hover {
  background: var(--ops-surface-muted);
}
.search-row-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.search-row-main strong {
  overflow: hidden;
  color: var(--ops-text);
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.search-row-sub {
  overflow: hidden;
  color: var(--ops-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.search-row-time {
  color: var(--ops-faint);
  font-family: var(--ops-font-mono);
  font-size: 10px;
  white-space: nowrap;
}
.search-row-arrow {
  color: var(--ops-muted);
}
@media (max-width: 720px) {
  .search-row {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .search-row-time {
    display: none;
  }
}
</style>
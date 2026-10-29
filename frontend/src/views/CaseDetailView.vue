<script setup lang="ts">
/**
 * 案例来源直达页。
 *
 * - 直接按路由 ID 调用 GET /api/cases/{id},不依赖案例列表、筛选结果或缓存;
 * - 支持直接打开、刷新、登录后回跳与前后退;切换 ID 时旧响应不得覆盖新记录;
 * - 403/404/非法 ID/网络错误分别展示清晰状态,失败时不残留上一个案例正文;
 * - 只读展示案例本身,不打开来源故障的原始日志、描述或生成历史(权限边界不变)。
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import { caseStatusMap, fmtTime } from '../labels'
import type { Case, CaseStatus } from '../types'

const route = useRoute()
const router = useRouter()

const item = ref<Case | null>(null)
const loading = ref(false)
const error = ref<{ kind: 'invalid' | 'forbidden' | 'not-found' | 'network' | 'server'; message: string } | null>(null)

// 请求序号:快速切换来源时,迟到响应不得覆盖新记录
let loadSeq = 0

function parseId(raw: unknown): number | null {
  if (typeof raw !== 'string' || !/^\d+$/.test(raw)) return null
  const id = Number(raw)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

async function load() {
  const id = parseId(route.params.id)
  const seq = ++loadSeq
  // 先清空旧内容:切换或失败时不能继续展示上一个来源正文
  item.value = null
  error.value = null
  if (id === null) {
    loading.value = false
    error.value = { kind: 'invalid', message: '来源地址无效:案例 ID 必须是正整数' }
    return
  }

  loading.value = true
  try {
    const { data } = await client.get<Case>(`/api/cases/${id}`)
    if (seq !== loadSeq) return
    item.value = data
  } catch (e) {
    if (seq !== loadSeq) return
    const status = axios.isAxiosError(e) ? e.response?.status : undefined
    if (status === 403) {
      error.value = { kind: 'forbidden', message: '当前账号无权查看该案例(案例不存在或不属于可见范围)' }
    } else if (status === 404) {
      error.value = { kind: 'not-found', message: '案例不存在或已被删除' }
    } else if (status === 400 || status === 422) {
      error.value = { kind: 'invalid', message: '案例 ID 无效' }
    } else if (!axios.isAxiosError(e) || !e.response) {
      error.value = { kind: 'network', message: `网络错误:${errMsg(e)}` }
    } else {
      error.value = { kind: 'server', message: errMsg(e) || '加载案例失败' }
    }
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

watch(
  () => route.params.id,
  () => {
    void load()
  },
  { immediate: true },
)

function goBack() {
  // 直接打开(新标签页/登录回跳)时回案例库;来自案例库等其他站内页面则按历史返回。
  // 不用 window.history.length:新标签页也可能有历史项,会把用户带离应用。
  const back = (router.options.history.state as { back?: string | null } | undefined)?.back
  if (back && !back.startsWith('/login')) router.back()
  else void router.push('/cases')
}

const statusInfo = computed(() => {
  const s = item.value?.status
  if (!s) return null
  if (s === 'approved') {
    return {
      type: 'success' as const,
      title: '已审核通过的正式诊断依据',
      desc: '本页只展示案例内容,不会打开来源故障的原始日志、私有描述或生成历史。',
    }
  }
  if (s === 'pending_review') {
    return {
      type: 'warning' as const,
      title: '待审核(非正式知识)',
      desc: '该案例尚未审核通过,不能作为正式诊断依据。',
    }
  }
  return {
    type: 'error' as const,
    title: '已退回(非正式知识)',
    desc: item.value?.review_comment
      ? `审核意见:${item.value.review_comment}`
      : '该案例已退回,不能作为正式诊断依据。',
  }
})

const canRetry = computed(
  () => error.value?.kind === 'network' || error.value?.kind === 'server',
)
</script>

<template>
  <div class="ops-page case-detail-page">
    <div class="ops-page-header">
      <div>
        <el-button text class="ops-back-link" @click="goBack">← 返回</el-button>
        <div class="ops-eyebrow">CASE FILE <span v-if="item">/ #{{ item.id }}</span></div>
        <h1>案例详情</h1>
        <p class="ops-page-description">从一次真实的处理过程，找到下一次排查的依据。</p>
      </div>
      <el-tag v-if="item" size="small" :type="(caseStatusMap[item.status as CaseStatus].type as any)">
        {{ caseStatusMap[item.status as CaseStatus].label }}
      </el-tag>
    </div>

    <div v-if="loading" class="ops-panel case-loading"><AppIcon name="files" :size="24" /><span>加载中…</span></div>
    <div v-else-if="error" class="ops-panel case-error" data-testid="case-detail-error">
      <el-alert :type="error.kind === 'forbidden' || error.kind === 'not-found' || error.kind === 'invalid' ? 'warning' : 'error'" :closable="false" show-icon :title="error.message" />
      <div class="mt-4 flex gap-2">
        <el-button v-if="canRetry" type="primary" plain @click="load">重试</el-button>
        <el-button @click="router.push('/cases')">返回案例库</el-button>
      </div>
    </div>

    <template v-else-if="item">
      <el-alert v-if="statusInfo" class="case-status-note" :type="statusInfo.type" :closable="false" :title="statusInfo.title" :description="statusInfo.desc" show-icon />
      <div class="case-reading-layout">
        <article class="ops-panel case-paper">
          <header class="case-paper-header">
            <span class="case-document-icon"><AppIcon name="book" :size="23" /></span>
            <div class="ops-eyebrow">KNOWLEDGE / INCIDENT CASE</div>
            <h2>{{ item.title }}</h2>
            <div v-if="item.tags?.length" class="case-tags"><el-tag v-for="tag in item.tags" :key="tag" size="small" type="info" effect="plain">{{ tag }}</el-tag></div>
          </header>
          <section class="case-document-section">
            <div class="case-section-label"><span>01</span><h3>现象(案例记录)</h3></div>
            <div class="case-prose">{{ item.symptom || '-' }}</div>
          </section>
          <section class="case-document-section case-confirmed-section">
            <div class="case-section-label"><span>02</span><h3>人工确认根因</h3><AppIcon name="check-circle" :size="17" /></div>
            <div class="case-prose">{{ item.confirmed_root_cause || '-' }}</div>
          </section>
          <section class="case-document-section">
            <div class="case-section-label"><span>03</span><h3>处理步骤</h3></div>
            <div class="case-prose">{{ item.handling_steps || '-' }}</div>
          </section>
          <section class="case-document-section">
            <div class="case-section-label"><span>04</span><h3>恢复验证</h3></div>
            <div class="case-prose">{{ item.recovery_verification || '-' }}</div>
          </section>
        </article>
        <aside class="case-metadata-column">
          <section class="ops-panel case-metadata-panel">
            <div class="ops-section-header"><h2>案例信息</h2><AppIcon name="files" :size="17" /></div>
            <dl class="case-metadata">
              <div><dt>环境</dt><dd>{{ item.environment || '-' }}</dd></div>
              <div><dt>服务</dt><dd>{{ item.service || '-' }}</dd></div>
              <div><dt>组件版本</dt><dd>{{ item.component_version || '-' }}</dd></div>
              <div><dt>更新时间</dt><dd>{{ fmtTime(item.updated_at) }}</dd></div>
              <div><dt>审核时间</dt><dd>{{ fmtTime(item.reviewed_at) }}</dd></div>
            </dl>
          </section>
          <section v-if="item.review_comment" class="ops-panel case-metadata-panel">
            <div class="ops-section-header"><h2>审核意见</h2><AppIcon name="shield" :size="17" /></div>
            <p class="case-review-comment">{{ item.review_comment }}</p>
          </section>
          <div class="case-reading-note"><AppIcon name="lock" :size="18" /><p>只读知识来源<br /><span>审核和内容维护请从案例库进入。原始故障的私有日志不在此页展示。</span></p></div>
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
.case-detail-page { max-width: 1380px; margin: 0 auto; }
.case-detail-page .ops-page-header { align-items: flex-end; }
.case-detail-page .ops-back-link { margin-bottom: 12px; }
.case-detail-page h1 { margin-top: 6px; }
.case-status-note { margin: 0 0 22px; }
.case-reading-layout { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 22px; align-items: start; }
.case-paper { padding: 32px 36px; }
.case-paper-header { padding-bottom: 24px; border-bottom: 1px solid var(--ops-border); }
.case-document-icon { display: grid; place-items: center; width: 48px; height: 48px; margin-bottom: 22px; border: 1px solid var(--ops-brand-line); border-radius: 5px; background: var(--ops-brand-soft); color: var(--ops-brand); }
.case-paper-header .ops-eyebrow { font-size: 10px; letter-spacing: 1.4px; }
.case-paper-header h2 { margin: 9px 0 0; color: var(--ops-text); font-size: 25px; font-weight: 650; line-height: 1.55; letter-spacing: -0.02em; }
.case-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 17px; }
.case-document-section { padding: 26px 0 0; }
.case-section-label { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.case-section-label > span { color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 11px; font-weight: 500; }
.case-section-label h3 { margin: 0; color: var(--ops-text); font-size: 14px; font-weight: 650; }
.case-section-label > svg { color: var(--ops-brand); }
.case-prose { color: var(--ops-text-soft); font-size: 14px; line-height: var(--ops-leading-reading); white-space: pre-wrap; overflow-wrap: anywhere; }
.case-confirmed-section { margin: 25px -17px 0; padding: 18px; border: 1px solid var(--ops-warm-line); border-radius: var(--ops-radius-panel); background: var(--ops-surface-warm); }
.case-confirmed-section .case-section-label > svg { color: var(--ops-success); }
.case-metadata-column { display: grid; gap: 18px; }
.case-metadata-panel { padding: 0; }
.case-metadata-panel h2 { margin: 0; font-size: 14px; font-weight: 650; }

.case-metadata-panel svg { color: var(--ops-faint); }
.case-metadata { margin: 0; padding: 20px 22px; }
.case-metadata > div { margin-bottom: 20px; }
.case-metadata > div:last-child { margin-bottom: 0; }
.case-metadata dt { margin-bottom: 6px; color: var(--ops-muted); font-size: 11px; }
.case-metadata dd { margin: 0; color: var(--ops-text); font-size: 13px; overflow-wrap: anywhere; }
.case-review-comment { margin: 0; padding: 18px 22px 22px; color: var(--ops-text-soft); font-size: 13px; line-height: 1.8; white-space: pre-wrap; }
.case-reading-note { display: flex; gap: 10px; padding: 4px 6px; color: var(--ops-muted); }
.case-reading-note > svg { flex-shrink: 0; margin-top: 3px; }
.case-reading-note p { margin: 0; color: var(--ops-text-soft); font-size: 12px; line-height: 1.8; }
.case-reading-note span { color: var(--ops-muted); }
.case-loading { display: flex; align-items: center; gap: 12px; padding: 32px; color: var(--ops-muted); }
.case-error { padding: 24px; }
@media (max-width: 1100px) {
  .case-reading-layout { grid-template-columns: minmax(0, 1fr) 240px; gap: 18px; }
  .case-paper { padding: 28px; }
}
@media (max-width: 900px) {
  .case-reading-layout { grid-template-columns: minmax(0, 1fr); }
  .case-metadata-column { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .case-paper { padding: 22px; }
  .case-paper-header h2 { font-size: 21px; }
  .case-metadata-column { grid-template-columns: minmax(0, 1fr); }
  .case-detail-page .ops-page-header { align-items: flex-start; }
}
</style>



<script setup lang="ts">
/**
 * 实体操作时间线:读取 /api/audit/entity,只显示当前用户有权查看的实体。
 * 详情只展示审核意见/状态等已有信息,不展示登录与网络数据。
 */
import { computed, onMounted, ref, watch } from 'vue'
import client, { errMsg } from '../api'
import AppIcon from './AppIcon.vue'
import { fmtTime } from '../labels'
import type { AuditTimelineItem } from '../types'

const props = defineProps<{
  entityType: 'fault' | 'case' | 'sop'
  entityId: number
  limit?: number
}>()

const items = ref<AuditTimelineItem[]>([])
const loading = ref(false)
const error = ref('')

const actionLabels: Record<string, string> = {
  'fault.create': '创建故障',
  'fault.create_from_alert': '告警生成故障',
  'fault.update': '更新故障信息',
  'fault.record_add': '追加处理记录',
  'fault.resolve': '人工确认解决并生成案例',
  'fault.resolve_case_updated': '重新确认解决并更新案例',
  'fault.sop_published': '衍生 SOP 已发布',
  'case.update': '更新案例',
  'case.submit': '提交审核',
  'case.approve': '审核通过',
  'case.return': '审核退回',
  'case.sop_published': '衍生 SOP 已发布',
  'sop.create': '创建 SOP',
  'sop.update': '更新 SOP',
  'sop.submit': '提交发布审核',
  'sop.publish': '发布 SOP',
  'sop.return': '审核退回',
  'sop.deprecate': '废弃 SOP',
  'sop.new_version': '派生新版本',
  'sop.generate_from_case': '从案例生成草稿',
  'sop.feedback': '提交使用反馈',
}

const summary = computed(() => items.value.slice(0, props.limit ?? 20))

function actionLabel(action: string): string {
  return actionLabels[action] ?? action
}

function detailText(item: AuditTimelineItem): string {
  const detail = item.detail ?? {}
  const comment = detail.comment ?? detail.reason
  if (typeof comment === 'string' && comment.trim()) return comment
  if (typeof detail.version === 'number' && actionLabel(item.action).includes('发布')) {
    return `版本 v${detail.version}`
  }
  if (typeof detail.root_cause === 'string' && detail.root_cause.trim()) {
    return `根因：${detail.root_cause}`
  }
  return ''
}

async function load() {
  if (!props.entityId) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await client.get<AuditTimelineItem[]>('/api/audit/entity', {
      params: { entity_type: props.entityType, entity_id: props.entityId, limit: props.limit ?? 20 },
    })
    items.value = Array.isArray(data) ? data : []
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => [props.entityType, props.entityId], load)
</script>

<template>
  <section class="ops-panel audit-timeline-panel">
    <div class="ops-section-header">
      <div class="audit-timeline-heading">
        <span class="audit-timeline-icon"><AppIcon name="history" :size="17" /></span>
        <div>
          <h2>操作时间线</h2>
          <p>状态流转与审核动作留痕</p>
        </div>
      </div>
      <span class="audit-timeline-count">{{ items.length }} 条</span>
    </div>
    <div v-if="loading" class="audit-timeline-empty">正在读取…</div>
    <div v-else-if="error" class="audit-timeline-empty">{{ error }}</div>
    <div v-else-if="!summary.length" class="audit-timeline-empty">暂无操作记录</div>
    <ol v-else class="audit-timeline-list">
      <li v-for="item in summary" :key="item.id" class="audit-timeline-item">
        <span class="audit-timeline-dot" aria-hidden="true"></span>
        <div class="audit-timeline-body">
          <div class="audit-timeline-line">
            <strong>{{ actionLabel(item.action) }}</strong>
            <span>{{ item.actor_name }}</span>
            <time>{{ fmtTime(item.created_at) }}</time>
          </div>
          <p v-if="detailText(item)" class="audit-timeline-detail">{{ detailText(item) }}</p>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.audit-timeline-panel { padding: 20px 22px; }
.audit-timeline-heading { display: flex; align-items: center; gap: 10px; }
.audit-timeline-heading h2 { margin: 0; font-size: 14px; font-weight: 650; color: var(--ops-text); }
.audit-timeline-heading p { margin: 2px 0 0; color: var(--ops-muted); font-size: 11px; }
.audit-timeline-icon { display: grid; place-items: center; width: 32px; height: 32px; border: 1px solid var(--ops-border); border-radius: 4px; background: var(--ops-surface-warm); color: var(--ops-muted); }
.audit-timeline-count { color: var(--ops-muted); font-family: var(--ops-font-mono); font-size: 10px; }
.audit-timeline-empty { padding: 18px 0 6px; color: var(--ops-muted); font-size: 12px; }
.audit-timeline-list { margin: 16px 0 0; padding: 0 0 0 14px; list-style: none; border-left: 1px solid var(--ops-border); }
.audit-timeline-item { position: relative; padding: 0 0 16px 16px; }
.audit-timeline-item:last-child { padding-bottom: 0; }
.audit-timeline-dot { position: absolute; top: 5px; left: -20px; width: 7px; height: 7px; border: 2px solid var(--ops-surface); border-radius: 50%; background: var(--ops-brand); box-shadow: 0 0 0 1px var(--ops-border); }
.audit-timeline-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; font-size: 12px; }
.audit-timeline-line strong { color: var(--ops-text); font-weight: 600; }
.audit-timeline-line span { color: var(--ops-muted); }
.audit-timeline-line time { margin-left: auto; color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 10px; }
.audit-timeline-detail { margin: 4px 0 0; color: var(--ops-text-soft); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
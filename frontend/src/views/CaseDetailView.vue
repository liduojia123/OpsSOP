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
  <div class="p-6 max-w-5xl">
    <div class="flex items-center gap-3 mb-4">
      <el-button text @click="goBack">← 返回</el-button>
      <h1 class="text-xl font-bold text-slate-800">案例详情</h1>
      <el-tag v-if="item" size="small" :type="(caseStatusMap[item.status as CaseStatus].type as any)">
        {{ caseStatusMap[item.status as CaseStatus].label }}
      </el-tag>
    </div>

    <div v-if="loading" class="page-card text-gray-500">加载中…</div>

    <div v-else-if="error" class="page-card" data-testid="case-detail-error">
      <el-alert
        :type="error.kind === 'forbidden' || error.kind === 'not-found' || error.kind === 'invalid' ? 'warning' : 'error'"
        :closable="false"
        show-icon
        :title="error.message"
      />
      <div class="mt-4 flex gap-2">
        <el-button v-if="canRetry" type="primary" plain @click="load">重试</el-button>
        <el-button @click="router.push('/cases')">返回案例库</el-button>
      </div>
    </div>

    <div v-else-if="item" class="space-y-4">
      <el-alert
        v-if="statusInfo"
        :type="statusInfo.type"
        :closable="false"
        :title="statusInfo.title"
        :description="statusInfo.desc"
      />

      <div class="page-card space-y-3 text-sm">
        <div>
          <div class="text-gray-500 mb-1">标题</div>
          <div class="font-medium text-slate-800">{{ item.title }}</div>
        </div>
        <div>
          <div class="text-gray-500 mb-1">现象(案例记录)</div>
          <div class="p-2 bg-slate-50 rounded whitespace-pre-wrap">{{ item.symptom || '-' }}</div>
        </div>
        <div>
          <div class="text-gray-500 mb-1">人工确认根因</div>
          <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ item.confirmed_root_cause || '-' }}</div>
        </div>
        <div>
          <div class="text-gray-500 mb-1">处理步骤</div>
          <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ item.handling_steps || '-' }}</div>
        </div>
        <div>
          <div class="text-gray-500 mb-1">恢复验证</div>
          <div class="p-2 bg-emerald-50 rounded whitespace-pre-wrap">{{ item.recovery_verification || '-' }}</div>
        </div>
        <div class="text-xs text-gray-400">
          环境:{{ item.environment || '-' }} · 服务:{{ item.service || '-' }} ·
          版本:{{ item.component_version || '-' }}
        </div>
        <div v-if="item.tags?.length" class="text-xs text-gray-400">
          标签:{{ item.tags.join('、') }}
        </div>
        <div class="text-xs text-gray-400 border-t border-slate-100 pt-2">
          审核时间:{{ fmtTime(item.reviewed_at) }} · 更新时间:{{ fmtTime(item.updated_at) }}
          <span v-if="item.review_comment"> · 审核意见:{{ item.review_comment }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

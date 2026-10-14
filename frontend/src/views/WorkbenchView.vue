<script setup lang="ts">
import { onMounted, ref } from 'vue'
import client from '../api'
import { caseStatusMap, faultStatusMap, fmtTime, sopStatusMap } from '../labels'
import type { Workbench } from '../types'

const wb = ref<Workbench | null>(null)
const loading = ref(true)

onMounted(async () => {
  try {
    const { data } = await client.get('/api/workbench')
    wb.value = data
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="p-6">
    <h1 class="text-xl font-bold text-slate-800 mb-4">工作台</h1>

    <div v-if="loading" class="text-gray-500">加载中…</div>

    <template v-else-if="wb">
      <!-- 指标卡 -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <div class="page-card">
          <div class="text-sm text-gray-500">我的待处理故障</div>
          <div class="text-3xl font-bold text-slate-800 mt-1">{{ wb.my_open_faults.length }}</div>
        </div>
        <div class="page-card">
          <div class="text-sm text-gray-500">待审核案例</div>
          <div class="text-3xl font-bold text-amber-600 mt-1">{{ wb.pending_case_reviews.length }}</div>
        </div>
        <div class="page-card">
          <div class="text-sm text-gray-500">待审核 SOP</div>
          <div class="text-3xl font-bold text-amber-600 mt-1">{{ wb.pending_sop_reviews.length }}</div>
        </div>
        <div class="page-card">
          <div class="text-sm text-gray-500">有效 SOP</div>
          <div class="text-3xl font-bold text-emerald-600 mt-1">{{ wb.published_sop_count }}</div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <!-- 我的待处理故障 -->
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">我的待处理故障</h2>
          <el-empty v-if="wb.my_open_faults.length === 0" description="暂无待处理故障" :image-size="60" />
          <div v-else class="space-y-2">
            <router-link
              v-for="f in wb.my_open_faults"
              :key="f.id"
              :to="`/faults/${f.id}`"
              class="block p-3 rounded border border-gray-100 hover:border-blue-300 hover:bg-blue-50/40"
            >
              <div class="flex items-center justify-between">
                <span class="font-medium text-slate-700">{{ f.title }}</span>
                <el-tag size="small" :type="(faultStatusMap[f.status].type as any)">
                  {{ faultStatusMap[f.status].label }}
                </el-tag>
              </div>
              <div class="text-xs text-gray-400 mt-1">{{ f.service || '未填服务' }} · 更新于 {{ fmtTime(f.updated_at) }}</div>
            </router-link>
          </div>
        </div>

        <!-- 待审核任务 -->
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">待审核任务</h2>
          <el-empty
            v-if="wb.pending_case_reviews.length === 0 && wb.pending_sop_reviews.length === 0"
            description="暂无待审核任务"
            :image-size="60"
          />
          <div v-else class="space-y-2">
            <router-link
              v-for="c in wb.pending_case_reviews"
              :key="`c${c.id}`"
              :to="`/cases`"
              class="block p-3 rounded border border-gray-100 hover:border-blue-300 hover:bg-blue-50/40"
            >
              <div class="flex items-center justify-between">
                <span class="font-medium text-slate-700">[案例] {{ c.title }}</span>
                <el-tag size="small" type="warning">{{ caseStatusMap[c.status].label }}</el-tag>
              </div>
            </router-link>
            <router-link
              v-for="s in wb.pending_sop_reviews"
              :key="`s${s.id}`"
              :to="`/sops/${s.id}`"
              class="block p-3 rounded border border-gray-100 hover:border-blue-300 hover:bg-blue-50/40"
            >
              <div class="flex items-center justify-between">
                <span class="font-medium text-slate-700">[SOP] {{ s.title }}</span>
                <el-tag size="small" type="warning">{{ sopStatusMap[s.status].label }}</el-tag>
              </div>
            </router-link>
          </div>
        </div>

        <!-- 最近更新 SOP -->
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">最近发布的 SOP</h2>
          <el-empty v-if="wb.recent_sops.length === 0" description="暂无已发布 SOP" :image-size="60" />
          <div v-else class="space-y-2">
            <router-link
              v-for="s in wb.recent_sops"
              :key="s.id"
              :to="`/sops/${s.id}`"
              class="block p-3 rounded border border-gray-100 hover:border-blue-300 hover:bg-blue-50/40"
            >
              <div class="font-medium text-slate-700">{{ s.title }}</div>
              <div class="text-xs text-gray-400 mt-1">v{{ s.version }} · 发布于 {{ fmtTime(s.published_at) }}</div>
            </router-link>
          </div>
        </div>

        <!-- 分类统计 -->
        <div class="page-card">
          <h2 class="font-semibold text-slate-700 mb-3">故障分类统计</h2>
          <el-empty
            v-if="wb.category_stats.every((c) => c.count === 0)"
            description="暂无故障数据"
            :image-size="60"
          />
          <div v-else class="space-y-2">
            <div
              v-for="c in wb.category_stats"
              :key="c.category_name"
              class="flex items-center justify-between text-sm"
            >
              <span class="text-slate-600">{{ c.category_name }}</span>
              <div class="flex items-center gap-2">
                <div class="w-32 bg-gray-100 rounded-full h-2">
                  <div
                    class="bg-blue-500 h-2 rounded-full"
                    :style="{
                      width: `${Math.min(100, c.count * 20)}%`,
                    }"
                  ></div>
                </div>
                <span class="w-6 text-right text-gray-500">{{ c.count }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 退回待修改 -->
      <div
        v-if="wb.my_returned_cases.length || wb.my_returned_sops.length"
        class="page-card mt-4 border-l-4 border-red-400"
      >
        <h2 class="font-semibold text-red-600 mb-3">被退回,待我修改</h2>
        <div class="space-y-2">
          <router-link
            v-for="c in wb.my_returned_cases"
            :key="`rc${c.id}`"
            :to="`/cases`"
            class="block p-2 rounded bg-red-50 text-sm"
          >
            [案例] {{ c.title }} — 审核意见: {{ c.review_comment }}
          </router-link>
          <router-link
            v-for="s in wb.my_returned_sops"
            :key="`rs${s.id}`"
            :to="`/sops/${s.id}`"
            class="block p-2 rounded bg-red-50 text-sm"
          >
            [SOP] {{ s.title }} — 审核意见: {{ s.review_comment }}
          </router-link>
        </div>
      </div>
    </template>
  </div>
</template>

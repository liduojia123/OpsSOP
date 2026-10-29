<script setup lang="ts">
/**
 * AI 模型设置页(用户自助开启 AI)。
 *
 * 页面骨架与系统管理/案例库一致:ops-page + ops-page-header + 面板。
 * - 三张状态卡:全局模式、我的生效配置、出网限制;
 * - 厂商表:三家厂商的配置状态、当前模型与接口地址;
 * - 配置表单:选厂商 → 填 Key → 拉取模型 → 保存。
 */
import { computed, onMounted, ref } from 'vue'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import AiProviderSettings from '../components/AiProviderSettings.vue'
import type { AiProvider, AiStatus } from '../types'

const status = ref<AiStatus | null>(null)
const providers = ref<AiProvider[]>([])
const selected = ref('')
const loading = ref(false)
const loadError = ref('')

const providerNames: Record<string, string> = {
  deepseek: 'DeepSeek',
  minimax: 'MiniMax',
  mimo: '小米 MiMo',
}

const globalModeLabel = computed(() => {
  const mode = status.value?.ai_mode ?? ''
  if (mode === 'disabled') return '未启用'
  if (mode === 'demo') return '演示模式'
  if (mode === 'local') return '本地模型'
  if (mode === 'cloud') return '云端模型'
  return '未知'
})

const effectiveLabel = computed(() => {
  if (!status.value?.user_ai_enabled) return '未配置 · 使用全局配置'
  return `${providerNames[status.value.user_provider] || status.value.user_provider} · 自填配置`
})

const configuredCount = computed(() => providers.value.filter((p) => p.has_key).length)

const pageChip = computed(() => {
  if (!status.value) return '状态读取中'
  if (status.value.user_ai_enabled) return `AI 已启用 · ${providerNames[status.value.user_provider] || status.value.user_provider}`
  return status.value.ai_mode === 'disabled' ? 'AI 未启用' : globalModeLabel.value
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [statusResp, providersResp] = await Promise.all([
      client.get('/api/system/ai-status'),
      client.get('/api/ai/providers'),
    ])
    status.value = statusResp.data
    providers.value = providersResp.data
  } catch (e) {
    loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ops-page">
    <header class="ops-page-header">
      <div>
        <div class="ops-eyebrow">AI / MODEL PROVIDERS</div>
        <h1>AI 设置</h1>
        <p class="ops-page-description">
          填写自己的 API Key 即可启用 AI 排查分析与 SOP 草稿生成；Key 只保存在你的账号下，页面不回显。
        </p>
      </div>
      <div class="ops-page-actions">
        <span class="ops-chip"><AppIcon name="bot" :size="15" />{{ pageChip }}</span>
        <el-button :loading="loading" @click="load"><AppIcon name="refresh" :size="15" />刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="loadError"
      class="mb-5"
      :title="loadError"
      type="error"
      :closable="false"
      show-icon
    />

    <div class="ai-stat-grid">
      <div class="ops-panel ai-stat">
        <div>
          <p class="ai-stat-label">全局 AI 模式</p>
          <p class="ai-stat-value">{{ globalModeLabel }}</p>
        </div>
        <span class="ops-icon-tile"><AppIcon name="server" :size="20" /></span>
      </div>
      <div class="ops-panel ai-stat">
        <div>
          <p class="ai-stat-label">我的生效配置</p>
          <p class="ai-stat-value">{{ effectiveLabel }}</p>
        </div>
        <span class="ops-icon-tile is-warm"><AppIcon name="bot" :size="20" /></span>
      </div>
      <div class="ops-panel ai-stat">
        <div>
          <p class="ai-stat-label">出网限制</p>
          <p class="ai-stat-value">{{ status?.local_only ? '仅本地 · 禁止云厂商' : '允许调用云端模型' }}</p>
        </div>
        <span class="ops-icon-tile"><AppIcon name="lock" :size="20" /></span>
      </div>
    </div>

    <section class="ops-panel ops-table-panel mb-6">
      <div class="ops-section-header">
        <div>
          <h2><AppIcon name="server" :size="17" />模型厂商</h2>
          <p>点「配置」切换厂商；模型 ID 与接口地址以厂商官方文档为准。</p>
        </div>
        <span class="ops-section-caption">已配置 {{ configuredCount }} / {{ providers.length }}</span>
      </div>
      <el-table :data="providers" empty-text="加载中">
        <el-table-column label="厂商" min-width="150">
          <template #default="{ row }">
            <span class="ai-vendor-name"><AppIcon name="bot" :size="16" />{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.has_key ? 'success' : 'info'">
              {{ row.has_key ? '已配置' : '未配置' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前模型" min-width="170">
          <template #default="{ row }">
            <span class="ops-id">{{ row.model || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="接口地址" min-width="240">
          <template #default="{ row }">
            <span class="ai-endpoint">{{ row.user_base_url || row.base_url }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="96" align="right">
          <template #default="{ row }">
            <el-button size="small" plain @click="selected = row.id">配置</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="ops-panel">
      <div class="ops-section-header">
        <div>
          <h2><AppIcon name="edit" :size="17" />配置我的模型</h2>
          <p>保存后，分析与 SOP 草稿生成使用这里选择的厂商与模型；删除配置即回落到全局配置。</p>
        </div>
      </div>
      <div class="ai-form-body">
        <p v-if="status" class="ai-form-note">{{ status.message }}</p>
        <AiProviderSettings :provider-id="selected" @changed="load" />
      </div>
    </section>
  </div>
</template>

<style scoped>
.ai-stat-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.ai-stat {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
}
.ai-stat-label {
  margin: 0;
  color: var(--ops-muted);
  font-size: 11px;
  line-height: 1.5;
}
.ai-stat-value {
  margin: 8px 0 0;
  color: var(--ops-text);
  font-size: 15px;
  font-weight: var(--ops-weight-semibold);
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.ai-vendor-name {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--ops-text);
  font-weight: var(--ops-weight-medium);
}
.ai-vendor-name > svg {
  color: var(--ops-brand);
}
.ai-endpoint {
  color: var(--ops-muted);
  font-family: var(--ops-font-mono);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ai-form-body {
  max-width: 760px;
  padding: 20px;
}
.ai-form-note {
  margin: 0 0 14px;
  color: var(--ops-muted);
  font-size: 11px;
  line-height: 1.7;
}
@media (max-width: 960px) {
  .ai-stat-grid {
    grid-template-columns: 1fr;
  }
}
</style>
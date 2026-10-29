<script setup lang="ts">
/**
 * AI 模型设置页(用户自助开启 AI)。
 *
 * - 一屏可见:当前状态、我已配置的厂商、配置表单;
 * - 填入自己的 API Key 后可实时拉取厂商可用模型并选择;
 * - Key 只保存在本人账号下,页面不回显;LOCAL_ONLY 时禁止云厂商。
 */
import { computed, onMounted, ref } from 'vue'
import client, { errMsg } from '../api'
import AppIcon from '../components/AppIcon.vue'
import AiProviderSettings from '../components/AiProviderSettings.vue'
import type { AiProvider, AiStatus } from '../types'

const status = ref<AiStatus | null>(null)
const providers = ref<AiProvider[]>([])
const selected = ref('')
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
  if (!status.value?.user_ai_enabled) return '未配置(使用全局配置)'
  return `${providerNames[status.value.user_provider] || status.value.user_provider} · 自填配置`
})

async function load() {
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
  }
}

onMounted(load)
</script>

<template>
  <div class="ai-settings-page">
    <header class="ops-page-header">
      <div>
        <h2>AI 模型设置</h2>
        <p class="ops-section-caption">
          填写你自己的 API Key 即可启用 AI 分析与 SOP 草稿生成;Key 只保存在你的账号下,页面不回显。
        </p>
      </div>
      <el-button :loading="!status" @click="load">刷新状态</el-button>
    </header>

    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon class="mb-3" />

    <section class="ops-panel ai-status-panel">
      <div class="ai-status-grid">
        <div>
          <span>全局 AI 模式</span>
          <strong>{{ globalModeLabel }}</strong>
        </div>
        <div>
          <span>我的生效配置</span>
          <strong>{{ effectiveLabel }}</strong>
        </div>
        <div>
          <span>出网限制</span>
          <strong>{{ status?.local_only ? 'LOCAL_ONLY 已开启(禁止云厂商)' : '允许调用云端模型' }}</strong>
        </div>
      </div>
      <p v-if="status" class="ops-note mt-2">{{ status.message }}</p>
    </section>

    <section class="ops-panel ai-providers-panel">
      <div class="ops-section-header">
        <div>
          <h3>支持的模型厂商</h3>
          <p class="ops-section-caption mt-1">点「配置」切换到对应厂商填写 Key;厂商与模型 ID 以官方文档为准。</p>
        </div>
      </div>
      <el-table :data="providers" empty-text="加载中">
        <el-table-column label="厂商" min-width="140">
          <template #default="{ row }">
            <span class="ai-provider-name">
              <AppIcon name="bot" :size="16" />{{ row.name }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.has_key ? 'success' : 'info'">
              {{ row.has_key ? '已配置' : '未配置' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前模型" min-width="160">
          <template #default="{ row }">{{ row.model || '-' }}</template>
        </el-table-column>
        <el-table-column label="接口地址" min-width="200">
          <template #default="{ row }"><span class="ops-id">{{ row.user_base_url || row.base_url }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="right">
          <template #default="{ row }">
            <el-button size="small" plain @click="selected = row.id">配置</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section class="ops-panel ai-form-panel">
      <div class="ops-section-header">
        <div>
          <h3>配置我的模型</h3>
          <p class="ops-section-caption mt-1">
            保存后，分析与 SOP 草稿生成会使用这里选择的厂商与模型;删除配置即回落到全局配置。
          </p>
        </div>
      </div>
      <AiProviderSettings :provider-id="selected" @changed="load" />
    </section>
  </div>
</template>

<style scoped>
.ai-settings-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.ops-page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.ai-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px 20px;
}
.ai-status-grid span {
  display: block;
  margin-bottom: 4px;
  color: var(--ops-muted);
  font-size: 11px;
}
.ai-status-grid strong {
  color: var(--ops-text-soft);
  font-weight: 600;
}
.ai-provider-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.ai-providers-panel,
.ai-form-panel {
  padding: 16px 20px;
}
</style>

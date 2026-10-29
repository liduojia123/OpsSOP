<script setup lang="ts">
/**
 * 管理员全局 AI 配置:直接在界面启用/关闭全局模型,不必改环境变量。
 *
 * - 全局配置对「没有自填 Key」的用户生效;用户自填配置优先;
 * - Key 加密保存,页面不回显;留空表示不修改已保存的 Key;
 * - 未保存 Key 时不能开启(与用户级开关一致)。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import type { AiGlobalConfig, AiModel, AiProvider } from '../types'

const emit = defineEmits<{ (e: 'changed'): void }>()

const providers = ref<AiProvider[]>([])
const config = ref<AiGlobalConfig | null>(null)
const providerId = ref('')
const apiKey = ref('')
const baseUrl = ref('')
const model = ref('')
const models = ref<AiModel[]>([])
const enabled = ref(false)
const loading = ref(false)
const saving = ref(false)
const loadingModels = ref(false)
const error = ref('')

const provider = computed(() => providers.value.find((p) => p.id === providerId.value) || null)
const hasKey = computed(() => config.value?.has_key ?? false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [globalResp, providersResp] = await Promise.all([
      client.get('/api/admin/ai'),
      client.get('/api/ai/providers'),
    ])
    config.value = globalResp.data
    providers.value = providersResp.data
    const saved = globalResp.data
    providerId.value = saved.provider || providersResp.data[0]?.id || ''
    baseUrl.value = saved.base_url || ''
    model.value = saved.model || ''
    enabled.value = Boolean(saved.enabled)
    apiKey.value = ''
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

async function fetchModels() {
  if (!providerId.value) return
  loadingModels.value = true
  error.value = ''
  try {
    const { data } = await client.post(`/api/ai/providers/${providerId.value}/models`, {
      api_key: apiKey.value,
      base_url: baseUrl.value,
    })
    models.value = data.models
    const preferred =
      data.models.find((m: AiModel) => (provider.value?.default_models || []).includes(m.id)) ||
      data.models[0]
    if (preferred && !data.models.some((m: AiModel) => m.id === model.value)) {
      model.value = preferred.id
    }
    if (data.source === 'fallback') {
      ElMessage.warning(data.note || '厂商未提供模型列表,已给出内置清单')
    }
  } catch (e) {
    models.value = []
    error.value = errMsg(e)
  } finally {
    loadingModels.value = false
  }
}

async function save() {
  if (!providerId.value) return
  saving.value = true
  error.value = ''
  try {
    const { data } = await client.put('/api/admin/ai', {
      enabled: enabled.value,
      provider: providerId.value,
      api_key: apiKey.value,
      base_url: baseUrl.value,
      model: model.value,
    })
    config.value = data
    apiKey.value = ''
    ElMessage.success(enabled.value ? '全局 AI 已启用' : '全局 AI 已关闭')
    emit('changed')
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="ai-global">
    <div class="ai-global-head">
      <div>
        <h3>全局 AI（管理员）</h3>
        <p>对没有自填 Key 的用户生效；用户自填配置优先。修改后立即生效，无需重启服务。</p>
      </div>
      <label class="ai-global-toggle">
        <span>{{ enabled ? '已启用' : '已关闭' }}</span>
        <el-switch v-model="enabled" :disabled="loading || saving || (!hasKey && !enabled)" />
      </label>
    </div>

    <div class="ai-global-grid">
      <div>
        <label class="form-label">模型厂商</label>
        <el-select v-model="providerId" class="w-full">
          <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
      </div>
      <div>
        <label class="form-label">API Key</label>
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          autocomplete="off"
          :placeholder="hasKey ? '已保存（留空表示不修改）' : `请输入 ${provider?.key_prefix || ''} 开头的 Key`"
        />
      </div>
      <div>
        <label class="form-label">接口地址（可选）</label>
        <el-input v-model="baseUrl" :placeholder="provider?.base_url || ''" />
      </div>
      <div>
        <label class="form-label">模型</label>
        <div class="ai-global-model">
          <el-button :loading="loadingModels" @click="fetchModels">获取模型</el-button>
          <el-select v-model="model" class="flex-1" placeholder="选择模型">
            <el-option v-for="m in models" :key="m.id" :label="m.id" :value="m.id" />
          </el-select>
        </div>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="mt-3" />

    <div class="ai-global-footer">
      <span v-if="config?.updated_at" class="ai-global-note">最近保存：{{ config.updated_at }}</span>
      <el-button type="primary" :loading="saving" @click="save">保存全局配置</el-button>
    </div>
  </div>
</template>

<style scoped>
.ai-global-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.ai-global-head h3 {
  margin: 0;
  color: var(--ops-text);
  font-size: var(--ops-text-base);
  font-weight: var(--ops-weight-semibold);
}
.ai-global-head p {
  margin: 4px 0 0;
  color: var(--ops-muted);
  font-size: 11px;
  line-height: 1.6;
}
.ai-global-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  color: var(--ops-text-soft);
  font-size: var(--ops-text-xs);
  white-space: nowrap;
}
.ai-global-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
  margin-top: 16px;
}
.ai-global-model {
  display: flex;
  gap: 8px;
  align-items: center;
}
.ai-global-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
}
.ai-global-note {
  color: var(--ops-faint);
  font-family: var(--ops-font-mono);
  font-size: 10px;
}
@media (max-width: 720px) {
  .ai-global-grid {
    grid-template-columns: 1fr;
  }
}
</style>
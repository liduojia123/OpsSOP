<script setup lang="ts">
/**
 * AI 模型厂商配置表单(用户自填 API Key)。
 *
 * - 支持 DeepSeek / MiniMax / 小米 MiMo,base_url 与模型列表以厂商官方文档为准;
 * - API Key 由用户本人填写:仅保存在用户自己的账号下,前端不回显已保存的 Key;
 * - 填好 Key 后点「获取模型」实时拉取该厂商可用模型,选择后保存;
 * - 已保存的 Key 再次保存时留空即可(不修改 Key)。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client, { errMsg } from '../api'
import type { AiModel, AiProvider } from '../types'

const emit = defineEmits<{ (e: 'changed'): void }>()

const props = defineProps<{ providerId?: string }>()

const providers = ref<AiProvider[]>([])
const currentId = ref('')
const apiKey = ref('')
const baseUrl = ref('')
const model = ref('')
const models = ref<AiModel[]>([])
const modelsSource = ref('')
const modelsNote = ref('')
const loadingProviders = ref(false)
const loadingModels = ref(false)
const saving = ref(false)
const error = ref('')

const provider = computed(() => providers.value.find((p) => p.id === currentId.value) || null)
const hasSavedKey = computed(() => provider.value?.has_key ?? false)

async function loadProviders() {
  loadingProviders.value = true
  error.value = ''
  try {
    const { data } = await client.get('/api/ai/providers')
    providers.value = data
    const wanted = props.providerId || currentId.value
    // 未显式指定时优先定位「已配置」的厂商,而不是列表第一个
    const configured = data.find((p: AiProvider) => p.has_key)
    const target = data.find((p: AiProvider) => p.id === wanted) || configured || data[0]
    if (target) selectProvider(target.id)
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    loadingProviders.value = false
  }
}

function selectProvider(id: string) {
  currentId.value = id
  const next = providers.value.find((p) => p.id === id)
  baseUrl.value = next?.user_base_url || ''
  model.value = next?.model || ''
  apiKey.value = ''
  models.value = []
  modelsNote.value = ''
  modelsSource.value = ''
}

async function fetchModels() {
  if (!currentId.value) return
  loadingModels.value = true
  error.value = ''
  try {
    const { data } = await client.post(`/api/ai/providers/${currentId.value}/models`, {
      api_key: apiKey.value,
      base_url: baseUrl.value,
    })
    models.value = data.models
    modelsSource.value = data.source
    modelsNote.value = data.note || ''
    if (data.models.length && !data.models.some((m: AiModel) => m.id === model.value)) {
      model.value = data.models[0].id
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
  if (!currentId.value) return
  saving.value = true
  error.value = ''
  try {
    await client.put(`/api/ai/providers/${currentId.value}/key`, {
      api_key: apiKey.value,
      base_url: baseUrl.value,
      model: model.value,
      enabled: true,
    })
    ElMessage.success('模型配置已保存')
    apiKey.value = ''
    await loadProviders()
    emit('changed')
  } catch (e) {
    error.value = errMsg(e)
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!currentId.value) return
  try {
    await ElMessageBox.confirm('确认删除该厂商的 API Key 配置?删除后分析将回落到全局配置。', '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await client.delete(`/api/ai/providers/${currentId.value}/key`)
    ElMessage.success('已删除')
    apiKey.value = ''
    models.value = []
    await loadProviders()
    emit('changed')
  } catch (e) {
    error.value = errMsg(e)
  }
}

watch(
  () => props.providerId,
  (value) => {
    if (value && value !== currentId.value) selectProvider(value)
  },
)

onMounted(loadProviders)
</script>

<template>
  <div class="ai-settings">
    <label class="form-label">模型厂商</label>
    <el-select v-model="currentId" class="ai-settings-field" :loading="loadingProviders" placeholder="选择厂商">
      <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
    </el-select>
    <p v-if="provider" class="ai-settings-hint">
      {{ provider.notes }}
      <a v-if="provider.docs_url" :href="provider.docs_url" target="_blank" rel="noreferrer">官方文档</a>
    </p>

    <div class="ai-settings-grid">
      <div>
        <label class="form-label">API Key</label>
        <el-input
          v-model="apiKey"
          type="password"
          show-password
          autocomplete="off"
          :placeholder="hasSavedKey ? '已保存（留空表示不修改）' : `请输入 ${provider?.key_prefix || ''} 开头的 Key`"
        />
      </div>
      <div>
        <label class="form-label">接口地址（可选）</label>
        <el-input v-model="baseUrl" :placeholder="provider?.base_url || ''" />
      </div>
    </div>
    <p v-if="provider && provider.alt_base_urls.length" class="ai-settings-hint">
      其他可用地址：{{ provider.alt_base_urls.join('、') }}
    </p>

    <div class="ai-settings-actions">
      <el-button :loading="loadingModels" @click="fetchModels">获取模型</el-button>
      <el-select v-model="model" class="ai-settings-model" placeholder="选择模型">
        <el-option v-for="m in models" :key="m.id" :label="m.id" :value="m.id" />
      </el-select>
    </div>
    <p v-if="modelsNote" class="ai-settings-hint">{{ modelsNote }}</p>
    <p v-else-if="modelsSource === 'provider' && models.length" class="ai-settings-hint">
      已从厂商接口获取 {{ models.length }} 个可用模型。
    </p>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="mt-2" />

    <div class="ai-settings-footer">
      <el-button type="danger" plain @click="remove" :disabled="!hasSavedKey">删除配置</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </div>
  </div>
</template>

<style scoped>
.ai-settings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ai-settings-field {
  width: 100%;
}
.ai-settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
  margin-top: 6px;
}
@media (max-width: 720px) {
  .ai-settings-grid {
    grid-template-columns: 1fr;
  }
}
.ai-settings-hint {
  font-size: 12px;
  color: var(--ops-muted);
  line-height: 1.6;
  margin: 0;
}
.ai-settings-hint a {
  color: var(--ops-accent);
  margin-left: 4px;
}
.ai-settings-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 4px;
}
.ai-settings-model {
  flex: 1;
  min-width: 0;
}
.ai-settings-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}
</style>

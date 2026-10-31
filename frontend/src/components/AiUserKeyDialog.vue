<script setup lang="ts">
/**
 * 「我的模型配置」弹窗:普通用户自助填写厂商 API Key。
 * - 用户级配置与开关,从系统管理的 AI 设置页签中开放给所有用户;
 * - 打开时读取当前启用状态;保存厂商配置后刷新状态并通知父级;
 * - 管理员的全局配置仍在「系统管理 → AI 设置」。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import AiProviderSettings from './AiProviderSettings.vue'
import type { AiStatus } from '../types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'changed'): void
}>()

const aiEnabled = ref(false)
// 内容首次打开才挂载:避免关闭状态下就发起请求
// (测试环境里未注册的 el-dialog 会被当普通元素渲染,内容会急切挂载偷跑请求)
const hasOpened = ref(false)
const showBody = computed(() => props.modelValue || hasOpened.value)

watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    hasOpened.value = true
    try {
      const { data } = await client.get<AiStatus>('/api/system/ai-status')
      aiEnabled.value = Boolean(data.user_ai_enabled)
    } catch {
      // 状态读不到时保持当前开关值,不影响 Key 配置
    }
  },
)

async function toggleAi(next: boolean) {
  try {
    await client.put('/api/ai/enabled', { enabled: next })
    ElMessage.success(next ? 'AI 已开启' : 'AI 已关闭')
    emit('changed')
  } catch (e) {
    aiEnabled.value = !next // 失败回滚开关
    ElMessage.error(errMsg(e))
  }
}

function onProviderChanged() {
  emit('changed')
  // 保存厂商配置通常会启用 AI:刷新开关状态
  void client
    .get<AiStatus>('/api/system/ai-status')
    .then(({ data }) => {
      aiEnabled.value = Boolean(data.user_ai_enabled)
    })
    .catch(() => {})
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="我的模型配置"
    width="600px"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="showBody">
      <div class="ai-key-toggle">
        <span>启用 AI（关闭后保留 Key 但不调用）</span>
        <el-switch v-model="aiEnabled" @change="(v: string | number | boolean) => toggleAi(Boolean(v))" />
      </div>
      <AiProviderSettings provider-id="" @changed="onProviderChanged" />
    </template>
    <p class="ai-key-note">
      API Key 加密保存在你的账号下，任何页面不回显；管理员的全局配置见「系统管理 → AI 设置」。
    </p>
  </el-dialog>
</template>

<style scoped>
.ai-key-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  color: var(--ops-text-soft);
  font-size: 12px;
}
.ai-key-note {
  margin: 10px 0 0;
  color: var(--ops-faint);
  font-size: 11px;
  line-height: 1.7;
}
</style>

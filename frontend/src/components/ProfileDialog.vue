<script setup lang="ts">
/**
 * 个人资料弹窗:头像、签名与密码。
 *
 * - 头像在浏览器内裁剪为 160×160 后转 JPEG data URL,避免上传大图;
 * - 服务端只接受 PNG/JPEG/WebP data URL 并校验文件头与大小;
 * - 修改密码会使当前会话失效,成功后退出到登录页。
 */
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import client, { errMsg } from '../api'
import AppIcon from './AppIcon.vue'
import { fmtTime, roleMap } from '../labels'
import { useAuthStore } from '../stores/auth'
import type { User } from '../types'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const auth = useAuthStore()
const router = useRouter()
const form = reactive({ signature: '', avatar: '' })
const avatarInput = ref<HTMLInputElement | null>(null)
const saving = ref(false)
const changingPassword = ref(false)
const passwordForm = reactive({ current: '', next: '', confirm: '' })

const user = computed<User | null>(() => auth.user)
const roleLabel = computed(() => (user.value ? roleMap[user.value.role] : ''))
const initial = computed(() => user.value?.display_name?.trim().slice(0, 1).toUpperCase() || 'U')
const preview = computed(() => form.avatar || '')

function close() {
  emit('update:modelValue', false)
}

function reset() {
  form.signature = user.value?.signature ?? ''
  form.avatar = user.value?.avatar ?? ''
  Object.assign(passwordForm, { current: '', next: '', confirm: '' })
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  },
  { immediate: true },
)

function pickAvatar() {
  avatarInput.value?.click()
}

function clearAvatar() {
  form.avatar = ''
  if (avatarInput.value) avatarInput.value.value = ''
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('图片读取失败'))
    image.src = url
  })
}

async function resizeAvatar(file: File): Promise<string> {
  const url = URL.createObjectURL(file)
  try {
    const image = await loadImage(url)
    const size = 160
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const context = canvas.getContext('2d')
    if (!context) throw new Error('当前浏览器不支持图片裁剪')
    // 居中裁成正方形,透明区域填暖白,避免转 JPEG 后变黑
    context.fillStyle = '#f8f6f0'
    context.fillRect(0, 0, size, size)
    const side = Math.min(image.width, image.height)
    const sx = (image.width - side) / 2
    const sy = (image.height - side) / 2
    context.drawImage(image, sx, sy, side, side, 0, 0, size, size)
    return canvas.toDataURL('image/jpeg', 0.86)
  } finally {
    URL.revokeObjectURL(url)
  }
}

async function onAvatarChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    ElMessage.warning('头像仅支持 PNG、JPEG 或 WebP 图片')
    input.value = ''
    return
  }
  if (file.size > 5 * 1024 * 1024) {
    ElMessage.warning('原图不能超过 5MB')
    input.value = ''
    return
  }
  try {
    form.avatar = await resizeAvatar(file)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    input.value = ''
  }
}

async function saveProfile() {
  if (saving.value) return
  saving.value = true
  try {
    const { data } = await client.patch<User>('/api/auth/profile', {
      signature: form.signature.trim(),
      avatar: form.avatar,
    })
    auth.updateUser(data)
    ElMessage.success('个人资料已更新')
    close()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    saving.value = false
  }
}

async function changePassword() {
  if (changingPassword.value) return
  if (!passwordForm.current || passwordForm.next.length < 8) {
    ElMessage.warning('请填写当前密码，新密码至少 8 个字符')
    return
  }
  if (passwordForm.next !== passwordForm.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  if (passwordForm.next === passwordForm.current) {
    ElMessage.warning('新密码不能与当前密码相同')
    return
  }
  changingPassword.value = true
  try {
    await client.post('/api/auth/change-password', {
      current_password: passwordForm.current,
      new_password: passwordForm.next,
    })
    auth.logout()
    Object.assign(passwordForm, { current: '', next: '', confirm: '' })
    close()
    ElMessage.success('密码已修改，请使用新密码重新登录')
    await router.replace({ name: 'login' })
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    changingPassword.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="个人资料"
    width="min(600px, 94vw)"
    class="ops-dialog profile-dialog"
    :close-on-click-modal="!saving && !changingPassword"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="profile-layout">
      <section class="profile-avatar-block">
        <button type="button" class="profile-avatar" :title="'更换头像'" @click="pickAvatar">
          <img v-if="preview" :src="preview" alt="当前头像" />
          <span v-else>{{ initial }}</span>
          <span class="profile-avatar-edit"><AppIcon name="edit" :size="14" /></span>
        </button>
        <input
          ref="avatarInput"
          class="profile-file"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          aria-label="选择头像图片"
          @change="onAvatarChange"
        />
        <div class="profile-avatar-actions">
          <el-button size="small" @click="pickAvatar">更换头像</el-button>
          <el-button v-if="preview" size="small" text @click="clearAvatar">恢复默认</el-button>
        </div>
        <p class="profile-hint">PNG / JPEG / WebP，自动裁剪为方形</p>
      </section>

      <section class="profile-fields">
        <div class="profile-identity">
          <strong>{{ user?.display_name || user?.username }}</strong>
          <span class="ops-id">{{ user?.username }} · {{ roleLabel }}</span>
        </div>
        <label class="form-label" for="profile-signature">一句话签名</label>
        <el-input
          id="profile-signature"
          v-model="form.signature"
          type="textarea"
          :rows="2"
          maxlength="200"
          show-word-limit
          placeholder="例如：先取证，再动手；把每次排查变成下一次的起点。"
        />
        <p class="profile-hint">签名会显示在左侧身份卡片中，最多 200 字。</p>
        <p v-if="user?.created_at" class="profile-meta">加入时间：{{ fmtTime(user.created_at) }}</p>
      </section>
    </div>

    <div class="profile-divider"></div>

    <section class="profile-password">
      <div class="profile-section-title">
        <span class="profile-section-icon"><AppIcon name="lock" :size="16" /></span>
        <div>
          <strong>修改密码</strong>
          <p>修改成功后所有已登录会话失效，需要使用新密码重新登录。</p>
        </div>
      </div>
      <el-form label-position="top" @submit.prevent="changePassword">
        <div class="profile-password-grid">
          <el-form-item label="当前密码">
            <el-input v-model="passwordForm.current" type="password" show-password autocomplete="current-password" />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="passwordForm.next" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="passwordForm.confirm" type="password" show-password autocomplete="new-password" />
          </el-form-item>
        </div>
      </el-form>
      <el-button :loading="changingPassword" :disabled="saving" @click="changePassword">修改密码</el-button>
    </section>

    <template #footer>
      <el-button :disabled="saving || changingPassword" @click="close">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="changingPassword" @click="saveProfile">
        保存资料
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.profile-layout { display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 24px; align-items: start; }
.profile-avatar-block { display: flex; flex-direction: column; align-items: center; gap: 9px; }
.profile-avatar { position: relative; width: 112px; height: 112px; padding: 0; overflow: hidden; border: 1px solid var(--ops-border); border-radius: 8px; background: var(--ops-surface-warm); color: var(--ops-brand); cursor: pointer; font-size: 40px; font-weight: 650; }
.profile-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
.profile-avatar-edit { position: absolute; right: 5px; bottom: 5px; display: grid; place-items: center; width: 24px; height: 24px; border-radius: 4px; background: rgb(34 36 40 / 78%); color: #fff; }
.profile-avatar:focus-visible { outline: 2px solid var(--ops-brand); outline-offset: 2px; }
.profile-file { display: none; }
.profile-avatar-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 4px; }
.profile-avatar-actions :deep(.el-button) { margin-left: 0; }
.profile-hint { margin: 0; color: var(--ops-muted); font-size: 11px; line-height: 1.6; text-align: center; }
.profile-fields { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.profile-identity { display: flex; flex-direction: column; gap: 3px; margin-bottom: 8px; }
.profile-identity strong { color: var(--ops-text); font-size: 17px; font-weight: 650; }
.profile-fields .form-label { margin-bottom: 0; }
.profile-meta { margin: 6px 0 0; color: var(--ops-faint); font-family: var(--ops-font-mono); font-size: 10px; }
.profile-divider { height: 1px; margin: 22px 0; background: var(--ops-border-light); }
.profile-section-title { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 14px; }
.profile-section-icon { display: grid; place-items: center; width: 30px; height: 30px; flex-shrink: 0; border: 1px solid var(--ops-border); border-radius: 4px; background: var(--ops-surface-warm); color: var(--ops-muted); }
.profile-section-title strong { color: var(--ops-text); font-size: 13px; }
.profile-section-title p { margin: 3px 0 0; color: var(--ops-muted); font-size: 11px; line-height: 1.6; }
.profile-password-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 14px; }
.profile-password :deep(.el-form-item) { margin-bottom: 12px; }
@media (max-width: 640px) {
  .profile-layout { grid-template-columns: minmax(0, 1fr); }
  .profile-avatar-block { align-items: flex-start; }
  .profile-avatar-actions { justify-content: flex-start; }
  .profile-password-grid { grid-template-columns: minmax(0, 1fr); }
  .profile-hint { text-align: left; }
}
</style>
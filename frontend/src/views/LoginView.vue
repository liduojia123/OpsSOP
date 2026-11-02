<script setup lang="ts">
import { nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppIcon from '../components/AppIcon.vue'
import AppLogo from '../components/AppLogo.vue'
import SignalBackdrop from '../components/SignalBackdrop.vue'
import EnergyProgress from '../components/EnergyProgress.vue'
import client, { errMsg } from '../api'
import { safeRedirect } from '../safeRedirect'
import { coverWorkspace, openWorkspace, cancelWorkspaceEntrance } from '../workspaceEntrance'
import { useAuthStore } from '../stores/auth'
import type { User } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const form = reactive({ username: '', password: '' })
const loading = ref(false)
const verified = ref(false)
const loginProgress = ref(0)
const controller = new AbortController()
let disposed = false
let waitTimer: ReturnType<typeof setTimeout> | undefined
let finishWait: (() => void) | undefined
let progressTimer: ReturnType<typeof setInterval> | undefined
async function waitForPreparation(ms: number) {
  if (ms <= 0) return
  await new Promise<void>(resolve => {
    finishWait = resolve
    waitTimer = setTimeout(resolve, ms)
  })
  waitTimer = undefined
  finishWait = undefined
}
onBeforeUnmount(() => {
  disposed = true
  controller.abort()
  clearTimeout(waitTimer)
  clearInterval(progressTimer)
  finishWait?.()
})

async function doLogin() {
  if (loading.value) return
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  verified.value = false
  loginProgress.value = 0
  const startedAt = Date.now()
  progressTimer = setInterval(() => {
    // 这是入场准备的定时进度，不冒充服务器验证进度；未验证成功最多到99%。
    loginProgress.value = Math.min(verified.value ? 100 : 99, Math.floor((Date.now() - startedAt) / 3000 * 100))
  }, 30)
  try {
    const { data } = await client.post('/api/auth/login', form, { signal: controller.signal })
    if (disposed) return
    verified.value = true
    // 三秒从点击时计算；慢验证不再额外等待，仍需验证成功才允许入场。
    const remaining = Math.max(0, 3000 - (Date.now() - startedAt))
    await waitForPreparation(remaining)
    if (disposed) return
    clearInterval(progressTimer)
    loginProgress.value = 100
    await nextTick()
    // 让满格100%实际呈现一帧以上，再覆盖入场画面。
    await waitForPreparation(150)
    if (disposed) return
    auth.setSession(data.token, data.user as User)
    ElMessage.success('欢迎，' + data.user.display_name)
    coverWorkspace()
    await nextTick()
    const target = safeRedirect(route.query.redirect)
    const failure = await router.replace(target || { name: 'workbench' })
    if (failure) { cancelWorkspaceEntrance(); return }
    await nextTick()
    openWorkspace()
  } catch (e) {
    if (disposed) return
    cancelWorkspaceEntrance()
    ElMessage.error(errMsg(e))
  } finally {
    clearInterval(progressTimer)
    loading.value = false
  }
}
</script>

<template>
  <div class="ops-login">
    <div v-if="loading" class="ops-login-feedback">
      <div class="ops-login-progress-caption" aria-hidden="true">
        <span>{{ verified ? '工作空间入场准备' : '登录验证与入场准备' }}</span>
        <span>{{ loginProgress }}%</span>
      </div>
      <EnergyProgress compact class="ops-login-progress ops-context-energy" :progress="loginProgress" :label="verified ? '工作空间入场准备' : '登录验证与入场准备'" />
    </div>
    <section class="ops-login-story">
      <SignalBackdrop />
      <div class="ops-login-brand">
        <AppLogo />
      </div>

      <div class="ops-login-story-main">
        <h1 class="ops-reveal" style="--ops-order: 1">每一次排查，<br />都有迹可循。</h1>

        <div class="ops-knowledge-map ops-reveal" style="--ops-order: 3" aria-label="从故障证据，经人工审核，到知识沉淀">
          <svg class="ops-knowledge-connections" viewBox="0 0 520 235" fill="none" aria-hidden="true">
            <circle cx="260" cy="122" r="101" stroke="#e2e7ee" />
            <circle cx="260" cy="122" r="71" stroke="#eff2f6" />
            <path d="M120 123H182C200 123 200 82 219 82H290C309 82 309 123 327 123H397" stroke="#B26C49" stroke-width="1.5" stroke-dasharray="4 5" />
            <circle cx="155" cy="123" r="3" fill="#E76338" />
            <circle cx="365" cy="123" r="3" fill="#E76338" />
          </svg>
          <div class="ops-map-node ops-map-evidence">
            <span class="ops-map-icon"><AppIcon name="activity" :size="23" /></span>
            <strong>故障证据</strong>
          </div>
          <div class="ops-map-node ops-map-review">
            <span class="ops-map-icon"><AppIcon name="shield" :size="23" /></span>
            <strong>人工审核</strong>
          </div>
          <div class="ops-map-node ops-map-knowledge">
            <span class="ops-map-icon"><AppIcon name="book" :size="23" /></span>
            <strong>知识沉淀</strong>
          </div>
        </div>
      </div>

    </section>

    <section class="ops-login-access">
      <div class="ops-login-mobile-brand">
        <AppLogo />
      </div>
      <div class="ops-login-form-wrap ops-reveal" style="--ops-order: 2">
        <div class="ops-login-form-heading">
          <h2>登录工作空间</h2>
        </div>
        <el-form class="ops-login-form" label-position="top" @submit.prevent="doLogin">
          <el-form-item label="用户名">
            <el-input v-model="form.username" placeholder="请输入用户名" autocomplete="username" size="large" @keyup.enter="doLogin">
              <template #prefix><AppIcon name="user" :size="17" /></template>
            </el-input>
          </el-form-item>
          <el-form-item label="密码">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              autocomplete="current-password"
              show-password
              size="large"
              @keyup.enter="doLogin"
            >
              <template #prefix><AppIcon name="lock" :size="17" /></template>
            </el-input>
          </el-form-item>
          <el-button type="primary" native-type="submit" class="ops-login-submit" size="large" :loading="loading">
            <span>登 录</span><AppIcon name="arrow-right" :size="18" />
          </el-button>
        </el-form>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ops-login-feedback { position: fixed; top: 64px; right: 24px; z-index: 3100; width: min(320px, calc(100vw - 48px)); pointer-events: none; }
.ops-login-progress-caption { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 9px; color: var(--ops-text); font-size: 12px; font-weight: 600; line-height: 18px; }
.ops-login-progress-caption > span:last-child { color: var(--ops-brand); font-family: var(--ops-font-mono); font-variant-numeric: tabular-nums; }
.ops-login-progress.is-compact { height: 24px; border-radius: 7px; }
.ops-login-progress :deep(.ops-energy-copy) { display: none; }
.ops-login { display: grid; grid-template-columns: 1.06fr 1fr; min-height: 100%; color: var(--ops-text); background: var(--ops-surface); }
.ops-login-story { position: relative; display: flex; flex-direction: column; min-height: 100vh; padding: 38px 8% 26px; overflow: hidden; background-color: #f8fafc; background-image: linear-gradient(#ffffff05 1px, transparent 1px), linear-gradient(90deg, #ffffff05 1px, transparent 1px); background-size: 40px 40px; color: var(--ops-text); }
.ops-login-brand, .ops-login-story-main { position: relative; z-index: 1; }
.ops-login-story { border-right: 1px solid var(--ops-border); }
.ops-login-brand, .ops-login-mobile-brand { --ops-logo-width: 210px; }
.ops-login-story-main { display: flex; flex: 1; flex-direction: column; justify-content: center; width: 100%; max-width: 520px; margin: 40px auto 20px; }
.ops-login-story h1 { margin: 22px 0 18px; color: var(--ops-text); font-size: clamp(40px, 4.3vw, 64px); font-weight: 650; line-height: 1.24; letter-spacing: -2.5px; }
.ops-knowledge-map { position: relative; height: 235px; margin: 28px -14px 0; }
.ops-knowledge-connections { position: absolute; inset: 0; width: 100%; height: 100%; }
.ops-map-node { position: absolute; display: flex; flex-direction: column; align-items: flex-start; width: 132px; padding: 16px; border: 1px solid var(--ops-border); border-radius: 8px; background: #ffffff; box-shadow: 0 10px 30px rgb(32 39 53 / 4%); }
.ops-map-icon { margin-bottom: 12px; color: #ec754c; }
.ops-map-node strong { color: var(--ops-text); font-size: 13px; font-weight: 600; }
.ops-map-review { border-color: var(--ops-brand-soft); background: var(--ops-brand-soft); transform: none; }
.ops-map-review strong { color: #292b2e; }
.ops-map-review .ops-map-icon { color: var(--ops-brand); }
.ops-map-evidence { top: 82px; left: 0; }
.ops-map-review { top: 33px; left: calc(50% - 66px); }
.ops-map-knowledge { top: 103px; right: 0; }
.ops-login-access { position: relative; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 70px 48px; background: #ffffff; }
.ops-login-form-wrap { width: 100%; max-width: 350px; padding-bottom: 10px; }
.ops-login-form-heading { margin-bottom: 34px; }
.ops-login-form-heading h2 { margin: 0 0 10px; color: var(--ops-text); font-size: 30px; font-weight: 650; letter-spacing: -1.3px; }
.ops-login-form :deep(.el-form-item) { margin-bottom: 22px; }
.ops-login-form :deep(.el-form-item__label) { margin-bottom: 8px; padding-bottom: 0; color: var(--ops-text-soft); font-size: 13px; font-weight: 500; }
.ops-login-form :deep(.el-input__wrapper) { min-height: 46px; padding-left: 13px; border-radius: 8px; background: #ffffff; }
.ops-login-form :deep(.el-input__prefix) { margin-right: 6px; color: var(--ops-faint); }
.ops-login-form :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--ops-brand) inset, 0 0 0 3px var(--ops-focus-ring); }
.ops-login-submit { width: 100%; height: 48px; margin-top: 7px; border-color: var(--ops-brand); border-radius: 8px; background: var(--ops-brand); }
.ops-login-submit:hover { border-color: var(--ops-brand); background: var(--ops-brand); }
.ops-login-submit :deep(> span) { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 0 2px; font-size: 14px; }
.ops-login-mobile-brand { display: none; }
@media (max-width: 1050px) {
  .ops-login-story { padding-right: 8%; padding-left: 8%; }
  .ops-map-node { width: 112px; padding: 13px 10px; }
  .ops-map-review { left: calc(50% - 56px); }
}
@media (max-width: 800px) {
  .ops-login { display: block; }
  .ops-login-story { display: none; }
  .ops-login-access { padding: 140px 28px 90px; background: var(--ops-canvas); }
  .ops-login-mobile-brand { position: absolute; top: 36px; left: 28px; display: flex; }
  .ops-login-form-wrap { max-width: 380px; }
}
@media (max-height: 700px) and (min-width: 801px) {
  .ops-login-story { padding-top: 28px; padding-bottom: 22px; }
  .ops-login-story-main { margin-top: 22px; }
  .ops-login-story h1 { margin-top: 14px; font-size: 40px; }
  .ops-knowledge-map { height: 210px; margin-top: 8px; }
}
.ops-knowledge-connections path { stroke-dasharray: 50 300; animation: ops-login-flow 7s linear infinite; }
@keyframes ops-login-flow { to { stroke-dashoffset: -350; } }
@media (prefers-reduced-motion: reduce) { .ops-knowledge-connections path { animation: none; } }
</style>

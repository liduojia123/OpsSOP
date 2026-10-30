<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppIcon from '../components/AppIcon.vue'
import AppLogo from '../components/AppLogo.vue'
import client, { errMsg } from '../api'
import { safeRedirect } from '../safeRedirect'
import { useAuthStore } from '../stores/auth'
import type { User } from '../types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function doLogin() {
  if (loading.value) return
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const { data } = await client.post('/api/auth/login', form)
    auth.setSession(data.token, data.user as User)
    ElMessage.success('欢迎，' + data.user.display_name)
    const target = safeRedirect(route.query.redirect)
    if (target) {
      await router.replace(target)
    } else {
      await router.replace({ name: 'workbench' })
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="ops-login">
    <section class="ops-login-story">
      <div class="ops-login-brand">
        <AppLogo dark />
      </div>

      <div class="ops-login-story-main">
        <span class="ops-login-eyebrow ops-reveal" style="--ops-order: 0">让经验，成为团队的知识资产</span>
        <h1 class="ops-reveal" style="--ops-order: 1">每一次排查，<br />都有迹可循。</h1>
        <p class="ops-login-intro ops-reveal" style="--ops-order: 2">把故障处理中的证据与经验，<br />沉淀为下一次可以复用的标准操作。</p>

        <div class="ops-knowledge-map ops-reveal" style="--ops-order: 3" aria-label="从故障证据，经人工审核，到知识沉淀">
          <svg class="ops-knowledge-connections" viewBox="0 0 520 235" fill="none" aria-hidden="true">
            <circle cx="260" cy="122" r="101" stroke="#4B4A45" />
            <circle cx="260" cy="122" r="71" stroke="#3D3C38" />
            <path d="M120 123H182C200 123 200 82 219 82H290C309 82 309 123 327 123H397" stroke="#B26C49" stroke-width="1.5" stroke-dasharray="4 5" />
            <circle cx="155" cy="123" r="3" fill="#E76338" />
            <circle cx="365" cy="123" r="3" fill="#E76338" />
          </svg>
          <div class="ops-map-node ops-map-evidence">
            <span class="ops-map-icon"><AppIcon name="activity" :size="23" /></span>
            <strong>故障证据</strong><small>记录事实与排查过程</small>
          </div>
          <div class="ops-map-node ops-map-review">
            <span class="ops-map-icon"><AppIcon name="shield" :size="23" /></span>
            <strong>人工审核</strong><small>确认经验与适用边界</small>
          </div>
          <div class="ops-map-node ops-map-knowledge">
            <span class="ops-map-icon"><AppIcon name="book" :size="23" /></span>
            <strong>知识沉淀</strong><small>发布为可复用 SOP</small>
          </div>
        </div>
      </div>

      <div class="ops-login-story-footer"><span></span>记录有据 · 知识可循</div>
    </section>

    <section class="ops-login-access">
      <div class="ops-login-mobile-brand">
        <AppLogo />
      </div>
      <div class="ops-login-form-wrap ops-reveal" style="--ops-order: 2">
        <div class="ops-login-form-heading">
          <span class="ops-login-form-icon"><AppIcon name="lock" :size="22" /></span>
          <h2>登录工作空间</h2>
          <p>连接故障处理与团队知识。</p>
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
        <p class="ops-login-account-note">账号由管理员分配。需要访问权限时，请联系团队管理员。</p>
      </div>
      <div class="ops-login-access-footer"><AppIcon name="shield" :size="14" /><span>运维故障知识库与 SOP 管理系统</span></div>
    </section>
  </div>
</template>

<style scoped>
.ops-login { display: grid; grid-template-columns: 1.06fr 1fr; min-height: 100%; color: var(--ops-text); background: var(--ops-surface); }
.ops-login-story { position: relative; display: flex; flex-direction: column; min-height: 100vh; padding: 38px 8% 26px; overflow: hidden; background-color: #222428; background-image: linear-gradient(#ffffff05 1px, transparent 1px), linear-gradient(90deg, #ffffff05 1px, transparent 1px); background-size: 40px 40px; color: #f6f1e5; }
.ops-login-story::after { content: 'FIELD NOTES / SYSTEM MEMORY'; position: absolute; top: 39px; right: 26px; color: #79776f; font-family: var(--ops-font-mono); font-size: 9px; letter-spacing: 2px; writing-mode: vertical-rl; }
.ops-login-brand, .ops-login-mobile-brand { --ops-logo-width: 210px; }
.ops-login-story-main { display: flex; flex: 1; flex-direction: column; justify-content: center; width: 100%; max-width: 520px; margin: 40px auto 20px; }
.ops-login-eyebrow { color: #e38965; font-family: var(--ops-font-mono); font-size: 11px; letter-spacing: 2px; }
.ops-login-story h1 { margin: 22px 0 18px; color: #f2ecdf; font-size: clamp(40px, 4.3vw, 64px); font-weight: 650; line-height: 1.24; letter-spacing: -2.5px; }
.ops-login-intro { margin: 0; color: #9d9c95; font-size: 13px; line-height: 1.9; }
.ops-knowledge-map { position: relative; height: 235px; margin: 28px -14px 0; }
.ops-knowledge-connections { position: absolute; inset: 0; width: 100%; height: 100%; }
.ops-map-node { position: absolute; display: flex; flex-direction: column; align-items: flex-start; width: 132px; padding: 16px; border: 1px solid #53514b; border-radius: 4px; background: #2a2c30; box-shadow: 8px 8px 0 #17191c; }
.ops-map-icon { margin-bottom: 12px; color: #ec754c; }
.ops-map-node strong { color: #e7dfcb; font-size: 13px; font-weight: 600; }
.ops-map-node small { margin-top: 5px; color: #979387; font-size: 9px; white-space: nowrap; }
.ops-map-review { border-color: #f0e8d6; background: #f0e8d6; transform: rotate(-5deg); }
.ops-map-review strong { color: #292b2e; }
.ops-map-review small { color: #827763; }
.ops-map-review .ops-map-icon { color: var(--ops-brand); }
.ops-map-evidence { top: 82px; left: 0; }
.ops-map-review { top: 33px; left: calc(50% - 66px); }
.ops-map-knowledge { top: 103px; right: 0; }
.ops-login-story-footer { display: flex; align-items: center; gap: 8px; color: #a79b84; font-family: var(--ops-font-mono); font-size: 10px; letter-spacing: 1px; }
.ops-login-story-footer > span { display: inline-block; width: 18px; height: 1px; background: #879b82; }
.ops-login-access { position: relative; display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 70px 48px; background: #f7f3e9; }
.ops-login-form-wrap { width: 100%; max-width: 350px; padding-bottom: 10px; }
.ops-login-form-heading { margin-bottom: 34px; }
.ops-login-form-icon { display: grid; place-items: center; width: 46px; height: 46px; margin-bottom: 24px; border: 1px solid #ded3be; border-radius: 3px; background: #e9e1d0; color: #a74a28; }
.ops-login-form-heading h2 { margin: 0 0 10px; color: var(--ops-text); font-size: 30px; font-weight: 650; letter-spacing: -1.3px; }
.ops-login-form-heading p { margin: 0; color: var(--ops-muted); font-size: 13px; }
.ops-login-form :deep(.el-form-item) { margin-bottom: 22px; }
.ops-login-form :deep(.el-form-item__label) { margin-bottom: 8px; padding-bottom: 0; color: var(--ops-text-soft); font-size: 13px; font-weight: 500; }
.ops-login-form :deep(.el-input__wrapper) { min-height: 46px; padding-left: 13px; border-radius: 4px; background: #fffdf6; }
.ops-login-form :deep(.el-input__prefix) { margin-right: 6px; color: var(--ops-faint); }
.ops-login-form :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px var(--ops-brand) inset, 0 0 0 3px var(--ops-focus-ring); }
.ops-login-submit { width: 100%; height: 48px; margin-top: 7px; border-color: #282a2d; border-radius: 4px; background: #282a2d; }
.ops-login-submit:hover { border-color: var(--ops-brand); background: var(--ops-brand); }
.ops-login-submit :deep(> span) { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 0 2px; font-size: 14px; }
.ops-login-account-note { margin: 22px 0 0; color: var(--ops-muted); font-size: 11px; line-height: 1.8; }
.ops-login-access-footer { position: absolute; right: 20px; bottom: 28px; left: 20px; display: flex; align-items: center; justify-content: center; gap: 6px; color: var(--ops-faint); font-size: 10px; }
.ops-login-mobile-brand { display: none; }
@media (max-width: 1050px) {
  .ops-login-story { padding-right: 8%; padding-left: 8%; }
  .ops-map-node { width: 112px; padding: 13px 10px; }
  .ops-map-review { left: calc(50% - 56px); }
  .ops-map-node small { font-size: 8px; }
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
</style>

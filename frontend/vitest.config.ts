import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端组件行为测试(与生产 vite.config.ts 分离,不影响构建产物)
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.spec.ts'],
  },
})

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 演示环境可用独立后端端口(如 BACKEND_PORT=18000),避免与既有项目冲突。
// 生产容器由 nginx 代理 /api,不依赖此变量。
const backendTarget = `http://127.0.0.1:${process.env.BACKEND_PORT || '8000'}`

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
  // 生产构建预览:与开发服务器同源代理,浏览器 E2E 不受 HMR 整页重载干扰
  preview: {
    port: 4173,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
})

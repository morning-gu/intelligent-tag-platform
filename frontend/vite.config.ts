import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 开发代理：/api 转发到本地 FastAPI 后端（免 CORS，前端仅写相对路径）
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    // jsdom 环境供组件测试（React 渲染 / window.location 断言）
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

// 前端工程: build 产物到 ../static, 由 Flask :9010 托管 (SPA catch-all).
// dev 期跑 :5173, 代理所有后端端点到 :9010.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:9010',
      '/render': 'http://localhost:9010',
      '/create_draft': 'http://localhost:9010',
      '/add_video': 'http://localhost:9010',
      '/add_text': 'http://localhost:9010',
      '/add_audio': 'http://localhost:9010',
      '/add_image': 'http://localhost:9010',
      '/save_draft': 'http://localhost:9010',
      '/perceive': 'http://localhost:9010',
      '/health': 'http://localhost:9010',
      '/get_': { target: 'http://localhost:9010', changeOrigin: true },
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
});

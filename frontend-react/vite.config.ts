import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { defineConfig } from 'vite';

// 前端工程: build 产物到 ../static, 由 Flask :9002 托管 (SPA catch-all).
// dev 期跑 :5173, 代理所有后端端点到 :9002.
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
      '/api': 'http://localhost:9002',
      '/render': 'http://localhost:9002',
      '/create_draft': 'http://localhost:9002',
      '/add_video': 'http://localhost:9002',
      '/add_text': 'http://localhost:9002',
      '/add_audio': 'http://localhost:9002',
      '/add_image': 'http://localhost:9002',
      '/save_draft': 'http://localhost:9002',
      '/perceive': 'http://localhost:9002',
      '/health': 'http://localhost:9002',
      '/get_': { target: 'http://localhost:9002', changeOrigin: true },
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
});

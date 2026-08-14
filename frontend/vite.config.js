import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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
      '/health': 'http://localhost:9002',
      '/get_': { target: 'http://localhost:9002', changeOrigin: true },
    }
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  }
})

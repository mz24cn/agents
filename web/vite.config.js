import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

function pad2(v) {
  return String(v).padStart(2, '0')
}

function buildTimestamp() {
  const now = new Date()
  const yy = String(now.getFullYear()).slice(2)
  const MM = pad2(now.getMonth() + 1)
  const dd = pad2(now.getDate())
  const hh = pad2(now.getHours())
  const mm = pad2(now.getMinutes())
  const ss = pad2(now.getSeconds())
  return `${yy}${MM}${dd}_${hh}${mm}${ss}`
}

export default defineConfig({
  define: {
    __BUILD_TIME__: JSON.stringify(buildTimestamp()),
  },
  plugins: [svelte()],
  server: {
    proxy: {
      '/v1': {
        target: 'http://localhost:7988',
        changeOrigin: true
      },
      '/ws': {
        target: 'ws://localhost:7988',
        ws: true
      }
    }
  },
  resolve: {
    alias: {
      '$lib': '/src/lib',
    }
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          // xterm JS (~334KB) 独立打包，Terminal.svelte 在 onMount 中动态 import，只有打开终端时才下载
          'xterm': [
            '@xterm/xterm',
            '@xterm/addon-fit'
          ]
        }
      }
    }
  },
  test: {
    environment: 'node',
    globals: false,
  }
})

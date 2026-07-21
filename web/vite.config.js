import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
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

import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { writeFileSync } from 'fs'
import { resolve } from 'path'

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

function buildVersionPlugin() {
  return {
    name: 'build-version',
    // writeBundle, not closeBundle: closeBundle also runs when the build
    // failed, so a syntax error while editing used to stamp a *newer*
    // dist/build_version over an untouched dist.  That stamp is the newest
    // entry in dist — i.e. what the runtime advertises as frontend_build — so
    // a failed build made the environment look like it had a frontend that
    // nothing on disk backed, and a pushed delta then carried the lone stamp.
    writeBundle() {
      const ts = buildTimestamp()
      writeFileSync(resolve(__dirname, 'dist/build_version'), ts, 'utf-8')
    }
  }
}

export default defineConfig({
  plugins: [svelte(), buildVersionPlugin()],
  server: {
    proxy: {
      '/v1/terminals/ws': {
        target: 'ws://localhost:7988',
        ws: true
      },
      // Tunnel browser bridge (terminal WS + HTTP) into registered children.
      '/v1/tunnel-proxy': {
        target: 'ws://localhost:7988',
        ws: true
      },
      '/v1': {
        target: 'http://localhost:7988',
        changeOrigin: true
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

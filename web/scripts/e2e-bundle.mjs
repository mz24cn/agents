// E2E bundle: 把共享 markdown 管道 + 图像缩放计算打进一个 IIFE（window.MD）
// i18n.svelte.js 用 shim 替换（e2e 只需字符串标签，t(k) 返回 key 即可）
import esbuild from 'esbuild'

const entry = '/tmp/e2e-md/entry.js'

const i18nShimPlugin = {
  name: 'i18n-shim',
  setup(build) {
    build.onResolve({ filter: /i18n\.svelte\.js$/ }, () => ({ path: 'i18n-shim', namespace: 'shim' }))
    build.onLoad({ filter: /.*/, namespace: 'shim' }, () => ({
      contents: 'export const t = (k) => k; export const i18n = { lang: "zh" }; export function setLang() {}',
      loader: 'js',
    }))
  },
}

await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  format: 'iife',
  globalName: 'MD',
  outfile: '/tmp/e2e-md/bundle.js',
  logLevel: 'silent',
  plugins: [i18nShimPlugin],
})
console.log('bundle written to /tmp/e2e-md/bundle.js')

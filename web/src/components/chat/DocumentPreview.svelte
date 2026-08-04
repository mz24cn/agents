<script>
  import { themeState } from '$lib/theme.svelte.js'
  import { t } from '$lib/i18n.svelte.js'

  /**
   * 文档预览组件（PDF / DOCX）
   *
   * CDN 库全部按需动态加载：
   * - PDF  用 dynamic import() 加载 pdfjs-dist@latest 的 legacy 构建
   *        （主构建依赖 ES2026 API `Map.prototype.getOrInsertComputed`，
   *         Chrome 140 及以下不支持；legacy 构建带转译 polyfill，兼容旧浏览器）
   * - DOCX 用 <script> 注入加载 jszip@latest + docx-preview@latest（UMD 全局）
   *
   * Props:
   * - file: { name, path } 待预览文件
   * - url:  内容下载地址（workspaceApi.content）
   */
  let { file = null, url = '' } = $props()

  const PDFJS_BASE = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@latest/legacy/build/'
  const JSZIP_SRC = 'https://cdn.jsdelivr.net/npm/jszip@latest/dist/jszip.min.js'
  const DOCX_PREVIEW_SRC = 'https://cdn.jsdelivr.net/npm/docx-preview@latest/dist/docx-preview.min.js'

  // 模块级缓存：同一会话内 CDN 库只加载一次，重复打开预览不重复下载
  let pdfjsPromise = null
  let docxLibsPromise = null

  // loading | ready | error
  let status = $state('loading')
  let errorMessage = $state('')
  let container

  let pdfDoc = null
  let pdfPages = [] // [{ pageNum, viewport, canvas }]
  let loadSeq = 0
  let redrawToken = 0

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const el = document.createElement('script')
      el.src = src
      el.onload = () => resolve()
      el.onerror = () => {
        el.remove()
        reject(new Error(`Failed to load ${src}`))
      }
      document.head.appendChild(el)
    })
  }

  function loadPdfjs() {
    if (!pdfjsPromise) {
      pdfjsPromise = import(PDFJS_BASE + 'pdf.min.mjs')
        .then((m) => {
          m.GlobalWorkerOptions.workerSrc = PDFJS_BASE + 'pdf.worker.min.mjs'
          return m
        })
        .catch((err) => {
          pdfjsPromise = null // 允许下次重试
          throw err
        })
    }
    return pdfjsPromise
  }

  function loadDocxLibs() {
    if (!docxLibsPromise) {
      docxLibsPromise = (async () => {
        await loadScript(JSZIP_SRC)
        await loadScript(DOCX_PREVIEW_SRC)
        if (!window.docx?.renderAsync) throw new Error('docx-preview unavailable')
        return window.docx
      })().catch((err) => {
        docxLibsPromise = null
        throw err
      })
    }
    return docxLibsPromise
  }

  function isPdf(name) {
    return /\.pdf$/i.test(name || '')
  }

  function isDocx(name) {
    return /\.docx$/i.test(name || '')
  }

  function pdfBackground() {
    return themeState.current === 'dark' ? '#1e1e1e' : '#ffffff'
  }

  async function renderPdf(pdfjs, data) {
    const doc = await pdfjs.getDocument({ data }).promise
    if (!container) {
      doc.destroy?.()
      return
    }
    pdfDoc = doc
    pdfPages = []
    const maxWidth = Math.max((container.clientWidth || 800) - 48, 200)
    for (let i = 1; i <= doc.numPages; i++) {
      if (!container) return
      const page = await doc.getPage(i)
      if (!container) return
      const base = page.getViewport({ scale: 1 })
      const scale = Math.min(maxWidth / base.width, 2)
      const viewport = page.getViewport({ scale })
      const canvas = document.createElement('canvas')
      canvas.width = Math.floor(viewport.width)
      canvas.height = Math.floor(viewport.height)
      canvas.className = 'doc-pdf-page'
      const wrap = document.createElement('div')
      wrap.className = 'doc-pdf-page-wrap'
      wrap.appendChild(canvas)
      container.appendChild(wrap)
      pdfPages.push({ pageNum: i, viewport, canvas })
      await page.render({ canvasContext: canvas.getContext('2d'), viewport, background: pdfBackground() }).promise
      page.cleanup?.()
    }
    status = 'ready'
  }

  async function renderDocx(docx, blob) {
    if (!container) return
    await docx.renderAsync(blob, container, undefined, {
      ignoreFonts: true,
      inWrapper: false,
    })
    status = 'ready'
  }

  // 主加载流程：file / url / container 任一变化时重跑
  $effect(() => {
    const c = container
    const f = file
    const u = url
    if (!c || !f || !u) return

    const seq = ++loadSeq
    status = 'loading'
    errorMessage = ''
    container.innerHTML = ''
    if (pdfDoc) {
      pdfDoc.destroy?.()
      pdfDoc = null
    }
    pdfPages = []

    const run = async () => {
      try {
        const res = await fetch(u)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        if (seq !== loadSeq || !container) return
        if (isPdf(f.name)) {
          const pdfjs = await loadPdfjs()
          if (seq !== loadSeq || !container) return
          const data = await res.arrayBuffer()
          if (seq !== loadSeq || !container) return
          await renderPdf(pdfjs, data)
        } else if (isDocx(f.name)) {
          const docx = await loadDocxLibs()
          if (seq !== loadSeq || !container) return
          const blob = await res.blob()
          if (seq !== loadSeq || !container) return
          await renderDocx(docx, blob)
        } else {
          throw new Error('unsupported file type')
        }
      } catch (err) {
        if (seq !== loadSeq) return
        errorMessage = err?.message || String(err)
        status = 'error'
      }
    }
    run()

    return () => {
      loadSeq++
      if (pdfDoc) {
        pdfDoc.destroy?.()
        pdfDoc = null
      }
      pdfPages = []
    }
  })

  // 主题切换时仅重绘已渲染的 PDF 页面（不重新解析文档）
  $effect(() => {
    void themeState.current
    const token = ++redrawToken
    if (!pdfDoc || pdfPages.length === 0) return
    const bg = pdfBackground()
    ;(async () => {
      for (const p of pdfPages) {
        if (token !== redrawToken) return
        try {
          const page = await pdfDoc.getPage(p.pageNum)
          if (token !== redrawToken) return
          await page.render({ canvasContext: p.canvas.getContext('2d'), viewport: p.viewport, background: bg }).promise
          page.cleanup?.()
        } catch {
          return
        }
      }
    })()
  })
</script>

<div class="doc-preview" bind:this={container}>
  {#if status === 'loading'}
    <div class="doc-preview-state">
      <span class="doc-preview-spinner"></span>
      <span>{t('previewLoading')}</span>
    </div>
  {:else if status === 'error'}
    <div class="doc-preview-state doc-preview-error">
      <div>⚠️ {t('previewFailed')}</div>
      <div class="doc-preview-error-detail">{t('previewFailedDetail')}</div>
      {#if errorMessage}
        <div class="doc-preview-error-detail">{errorMessage}</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .doc-preview {
    flex: 1;
    overflow: auto;
    padding: 16px;
    background: var(--bg);
  }

  .doc-preview-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    color: var(--text-secondary, var(--text));
    font-size: 0.9rem;
    min-height: 200px;
  }

  .doc-preview-spinner {
    width: 22px;
    height: 22px;
    border: 2px solid var(--border);
    border-top-color: var(--accent, #4a90d9);
    border-radius: 50%;
    animation: doc-preview-spin 0.8s linear infinite;
  }

  @keyframes doc-preview-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .doc-preview-error {
    color: var(--danger, #e5484d);
  }

  .doc-preview-error-detail {
    color: var(--text-secondary, var(--text));
    font-size: 0.8rem;
    word-break: break-all;
    max-width: 80%;
    text-align: center;
  }

  :global(.doc-pdf-page-wrap) {
    display: flex;
    justify-content: center;
    margin-bottom: 16px;
  }

  :global(.doc-pdf-page) {
    max-width: 100%;
    height: auto;
    background: #ffffff;
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.25);
    border-radius: 2px;
  }

  :global([data-theme='dark'] .doc-pdf-page) {
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.6);
  }

  /* docx-preview 生成的文档结构微调（inWrapper: false 时直接渲染 section.docx） */
  :global(.docx-wrapper) {
    background: transparent !important;
    padding: 0 !important;
  }

  :global(.docx-wrapper > section.docx),
  :global(.doc-preview > section.docx) {
    box-shadow: 0 1px 6px rgba(0, 0, 0, 0.2);
    margin: 0 auto 16px;
  }
</style>

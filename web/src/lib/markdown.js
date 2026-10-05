/**
 * Markdown 渲染共享管道（唯一入口）
 *
 * 聊天消息（MarkdownRenderer.svelte）与文件管理器 MD 预览
 * （WorkspaceFileManager.svelte）都必须走这里，避免两套 marked 配置漂移
 * （历史上文件预览漏接 mermaid/math/sanitize 就是这种漂移的产物）。
 *
 * 管道：CRLF 归一化 → extractMath（$…$/$$…$$ 占位）→ extractMermaidFences
 *      （```mermaid 占位）→ marked.parse（共享 Renderer）→ sanitizeHtml。
 * 挂载后调 bindMarkdownExtras(containerEl) 绑定复制按钮、本地渲染 mermaid、
 * 渲染 KaTeX 数学（均按需加载 CDN，幂等）。
 */
import { marked } from 'marked'
import { highlight, escapeHtml } from './highlight.js'
import { t } from './i18n.svelte.js'
import { copyToClipboard } from './clipboard.js'
import { extractMath, renderMathElements } from './math.js'
import { extractMermaidFences, renderMermaidElements } from './mermaid.js'

/**
 * 简易 HTML 消毒：marked 默认放行原始 HTML，而产物会经 {@html} 注入 DOM。
 * 聊天内容来自模型、文件预览内容来自磁盘，两者都需要这层防护。
 */
export function sanitizeHtml(html) {
  return html
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
    .replace(/\bon\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .replace(/javascript\s*:/gi, 'about:blank')
}

/**
 * 构建共享的 marked.Renderer。
 * @param {{ resolveImageSrc?: (src: string) => string }} options
 *   resolveImageSrc - 图片 src 解析钩子（文件预览用来把相对路径解析成
 *   workspace API URL）；不提供时原样保留（聊天场景）。
 */
export function createSharedRenderer({ resolveImageSrc = null } = {}) {
  const renderer = new marked.Renderer()

  // 代码块：语言角标 + 语法高亮 + 复制按钮（原始代码 Base64 内联，点击即拷）
  renderer.code = function ({ text, lang }) {
    const normalizedLang = (lang || '').toLowerCase()
    const highlightedHtml = highlight(text, normalizedLang)
    const rawBase64 = btoa(unescape(encodeURIComponent(text)))
    const langLabel = normalizedLang
      ? `<span class="code-lang">${normalizedLang.toUpperCase()}</span>`
      : ''
    const copyBtn = `<button class="copy-btn" data-copy-btn data-raw-code="${rawBase64}">${t('copy')}</button>`
    return `<div class="code-block">${copyBtn}${langLabel}<pre><code class="${normalizedLang ? 'language-' + normalizedLang : ''}">${highlightedHtml}</code></pre></div>`
  }

  // 链接：新标签页打开
  renderer.link = function ({ href, title, text }) {
    const titleAttr = title ? ` title="${title}"` : ''
    return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`
  }

  // 图片：可选的 src 解析钩子（markdown 内相对路径图片）
  if (resolveImageSrc) {
    renderer.image = function ({ href, title, text }) {
      const titleAttr = title ? ` title="${title}"` : ''
      const src = resolveImageSrc(href)
      return `<img src="${src}" alt="${text}"${titleAttr} />`
    }
  }

  return renderer
}

/**
 * 渲染 markdown 源码为 HTML 字符串（唯一管道）。
 * @param {string} src markdown 原文
 * @param {{ resolveImageSrc?: (src: string) => string }} [options]
 * @returns {string} HTML；marked 异常时回退为转义纯文本
 */
export function renderMarkdown(src, { resolveImageSrc = null } = {}) {
  if (!src) return ''
  // CRLF / CR → LF：Windows 文件预览时 \r 会漏进 <pre> 造成额外空行
  const normalized = src.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  try {
    const renderer = createSharedRenderer({ resolveImageSrc })
    const raw = marked.parse(extractMermaidFences(extractMath(normalized)), {
      renderer,
      gfm: true,
      breaks: true,
    })
    return sanitizeHtml(raw)
  } catch {
    return escapeHtml(normalized)
  }
}

/**
 * 挂载后绑定动态能力（幂等，可重复调用）：
 * 1. 代码块复制按钮
 * 2. mermaid 占位符本地渲染（含工具栏）
 * 3. KaTeX 数学占位符渲染
 * @param {HTMLElement|null} containerEl 含 {@html} 产物的容器
 */
export function bindMarkdownExtras(containerEl) {
  if (!containerEl) return

  // --- 复制按钮 ---
  const buttons = containerEl.querySelectorAll('[data-copy-btn]')
  buttons.forEach((btn) => {
    if (btn.dataset.copyBound) return
    btn.dataset.copyBound = '1'
    btn.addEventListener('click', () => {
      const rawCode = btn.getAttribute('data-raw-code')
      if (!rawCode) return
      try {
        copyToClipboard(decodeURIComponent(escape(atob(rawCode))))
      } catch {
        // Silent failure
      }
    })
  })

  // --- Mermaid 图表：mermaid UMD CDN 按需本地渲染（零打包成本，无 CORS 问题）---
  const mermaidEls = containerEl.querySelectorAll('.mermaid-pending:not([data-mermaid-done])')
  if (mermaidEls.length) {
    void renderMermaidElements([...mermaidEls], {
      labels: {
        zoomIn: t('mermaidZoomIn'),
        zoomOut: t('mermaidZoomOut'),
        downloadSvg: t('mermaidDownloadSvg'),
        downloadPng: t('mermaidDownloadPng'),
      },
    })
  }

  // --- Math：KaTeX CDN 按需渲染 ---
  const mathEls = containerEl.querySelectorAll('.math-pending:not([data-math-done])')
  if (mathEls.length) void renderMathElements([...mathEls])
}

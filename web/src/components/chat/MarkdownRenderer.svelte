<script>
  import { marked } from 'marked'
  import { highlight } from '$lib/highlight'
  import { t } from '$lib/i18n.svelte.js'

  let { content = '' } = $props()

  // Simple HTML escaper for XSS prevention — applied after marked renders
  function sanitizeHtml(html) {
    // Remove script tags and event handlers
    return html
      .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
      .replace(/\bon\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '')
      .replace(/javascript\s*:/gi, 'about:blank')
  }

  // Configure marked with GFM (tables, strikethrough) and custom renderer
  const renderer = new marked.Renderer()

  // Code blocks: render with language label, syntax highlighting, and copy button
  renderer.code = function ({ text, lang }) {
    const normalizedLang = lang || ''
    const highlightedHtml = highlight(text, normalizedLang)
    // Encode raw text as base64 for safe embedding in HTML attribute (UTF-8 safe)
    const rawBase64 = btoa(unescape(encodeURIComponent(text)))
    const langLabel = normalizedLang
      ? `<span class="code-lang">${normalizedLang.toUpperCase()}</span>`
      : ''
    const copyBtn = `<button class="copy-btn" data-copy-btn data-raw-code="${rawBase64}">${t('copy')}</button>`
    return `<div class="code-block">${copyBtn}${langLabel}<pre><code class="${normalizedLang ? 'language-' + normalizedLang : ''}">${highlightedHtml}</code></pre></div>`
  }

  // Links: open in new tab
  renderer.link = function ({ href, title, text }) {
    const titleAttr = title ? ` title="${title}"` : ''
    return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`
  }

  const markedOptions = {
    renderer,
    gfm: true,
    breaks: true,
  }

  function renderMarkdown(src) {
    if (!src) return ''
    try {
      const raw = marked.parse(src, markedOptions)
      return sanitizeHtml(raw)
    } catch {
      // Fallback to escaped plain text
      return src
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
    }
  }

  let html = $derived(renderMarkdown(content))

  // Bind copy button click events whenever rendered HTML updates
  let markdownContainer

  $effect(() => {
    // Access html to make this effect re-run when content changes
    void html
    if (!markdownContainer) return

    // Use a microtask to ensure the DOM has been updated
    Promise.resolve().then(() => {
      const buttons = markdownContainer.querySelectorAll('[data-copy-btn]')
      buttons.forEach((btn) => {
        // Avoid double-binding by checking a flag
        if (btn.dataset.copyBound) return
        btn.dataset.copyBound = '1'
        btn.addEventListener('click', () => {
          const rawCode = btn.getAttribute('data-raw-code')
          if (!rawCode) return
          try {
            const decoded = decodeURIComponent(escape(atob(rawCode)))
            navigator.clipboard.writeText(decoded).catch(() => {})
          } catch {
            // Silent failure: base64 decode failed
          }
        })
      })
    })
  })
</script>

<div class="markdown-content" bind:this={markdownContainer}>
  {@html html}
</div>

<style>
  .markdown-content {
    line-height: 1.6;
    font-size: 0.9rem;
    word-break: break-word;
  }

  /* Headings */
  .markdown-content :global(h1),
  .markdown-content :global(h2),
  .markdown-content :global(h3),
  .markdown-content :global(h4),
  .markdown-content :global(h5),
  .markdown-content :global(h6) {
    margin: 0.8em 0 0.4em;
    font-weight: 600;
    line-height: 1.3;
  }
  .markdown-content :global(h1) { font-size: 1.4em; }
  .markdown-content :global(h2) { font-size: 1.25em; }
  .markdown-content :global(h3) { font-size: 1.1em; }

  /* Paragraphs */
  .markdown-content :global(p) {
    margin: 0.4em 0;
  }

  /* Lists */
  .markdown-content :global(ul),
  .markdown-content :global(ol) {
    margin: 0.4em 0;
    padding-left: 1.5em;
  }
  .markdown-content :global(li) {
    margin: 0.2em 0;
  }

  /* Links */
  .markdown-content :global(a) {
    color: var(--primary, #4a9eff);
    text-decoration: underline;
  }

  /* Inline code */
  .markdown-content :global(code) {
    background: var(--bg-tertiary, rgba(0,0,0,0.1));
    padding: 0.15em 0.35em;
    border-radius: 3px;
    font-size: 0.88em;
    font-family: 'Fira Code', 'Consolas', monospace;
  }

  /* Code blocks */
  .markdown-content :global(.code-block) {
    position: relative;
    margin: 0.6em 0;
  }
  /* 语言标签：右下角 */
  .markdown-content :global(.code-lang) {
    position: absolute;
    bottom: 0;
    right: 0;
    padding: 2px 8px;
    font-size: 0.7em;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border-radius: 4px 0 4px 0;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    z-index: 1;
  }
  .markdown-content :global(.code-footer) {
    display: flex;
    justify-content: flex-end;
    padding: 2px 4px;
  }
  /* 复制按钮：代码块内右上角 */
  .markdown-content :global(.code-block > .copy-btn) {
    position: absolute;
    top: 0;
    right: 0;
    z-index: 1;
  }
  :global(.copy-btn) {
    padding: 2px 8px;
    font-size: 0.7em;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border: none;
    border-radius: 0 4px 0 4px;
    cursor: pointer;
    line-height: 1.4;
    transition: color 0.1s, background 0.1s;
  }
  :global(.copy-btn:hover) {
    color: var(--text-primary, #333);
    background: var(--bg-secondary, rgba(0,0,0,0.2));
  }
  :global(.copy-btn:active) {
    background: var(--primary, #4a9eff);
    color: #fff;
  }
  .markdown-content :global(pre) {
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    padding: 0.8em 1em;
    border-radius: 4px;
    overflow-x: auto;
    margin: 0;
  }
  .markdown-content :global(pre code) {
    background: none;
    padding: 0;
    font-size: 0.85em;
    line-height: 1.5;
  }

  /* Syntax highlighting - dark theme (default) */
  .markdown-content :global(.hl-keyword) { color: #c792ea; }
  .markdown-content :global(.hl-string)  { color: #c3e88d; }
  .markdown-content :global(.hl-comment) { color: #546e7a; font-style: italic; }
  .markdown-content :global(.hl-number)  { color: #f78c6c; }
  .markdown-content :global(.hl-boolean) { color: #ff5874; }
  .markdown-content :global(.hl-null)    { color: #ff5874; }
  .markdown-content :global(.hl-key)     { color: #82aaff; }
  .markdown-content :global(.hl-variable){ color: #f07178; }

  /* Syntax highlighting - light theme overrides */
  :root[data-theme="light"] .markdown-content :global(.hl-keyword) { color: #7c3aed; }
  :root[data-theme="light"] .markdown-content :global(.hl-string)  { color: #16a34a; }
  :root[data-theme="light"] .markdown-content :global(.hl-comment) { color: #6b7280; font-style: italic; }
  :root[data-theme="light"] .markdown-content :global(.hl-number)  { color: #c2410c; }
  :root[data-theme="light"] .markdown-content :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .markdown-content :global(.hl-null)    { color: #dc2626; }
  :root[data-theme="light"] .markdown-content :global(.hl-key)     { color: #1d4ed8; }
  :root[data-theme="light"] .markdown-content :global(.hl-variable){ color: #b45309; }

  /* Blockquotes */
  .markdown-content :global(blockquote) {
    margin: 0.5em 0;
    padding: 0.3em 0.8em;
    border-left: 3px solid var(--primary, #4a9eff);
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.04));
    border-radius: 0 4px 4px 0;
  }

  /* Tables */
  .markdown-content :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 0.6em 0;
    font-size: 0.88em;
  }
  .markdown-content :global(th),
  .markdown-content :global(td) {
    border: 1px solid var(--border, #ddd);
    padding: 0.4em 0.7em;
    text-align: left;
  }
  .markdown-content :global(th) {
    background: var(--bg-tertiary, rgba(0,0,0,0.06));
    font-weight: 600;
  }

  /* Horizontal rule */
  .markdown-content :global(hr) {
    border: none;
    border-top: 1px solid var(--border, #ddd);
    margin: 0.8em 0;
  }

  /* Strong / Em */
  .markdown-content :global(strong) {
    font-weight: 600;
  }

  /* Images */
  .markdown-content :global(img) {
    max-width: 100%;
    border-radius: 4px;
  }
</style>

<script>
  // 渲染管道统一走 $lib/markdown.js（与文件管理器 MD 预览共用），
  // 嵌入内容样式在 $lib/markdown.css（.md-view 作用域）。
  import { renderMarkdown, bindMarkdownExtras } from '$lib/markdown.js'

  let { content = '' } = $props()

  let html = $derived(renderMarkdown(content))

  let markdownContainer

  $effect(() => {
    void html
    if (!markdownContainer) return
    Promise.resolve().then(() => {
      if (!markdownContainer) return
      // 复制按钮 + mermaid 本地渲染 + KaTeX 数学渲染（幂等，见 bindMarkdownExtras）
      bindMarkdownExtras(markdownContainer)
    })
  })
</script>

<div class="markdown-content md-view" bind:this={markdownContainer}>
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

  .markdown-content :global(p) { margin: 0.4em 0; }

  .markdown-content :global(ul),
  .markdown-content :global(ol) {
    margin: 0.4em 0;
    padding-left: 1.5em;
  }
  .markdown-content :global(li) { margin: 0.2em 0; }

  .markdown-content :global(a) {
    color: var(--primary, #4a9eff);
    text-decoration: underline;
  }

  .markdown-content :global(code) {
    background: var(--bg-secondary, rgba(0,0,0,0.1));
    padding: 0.15em 0.35em;
    border-radius: 3px;
    font-size: 0.88em;
    font-family: 'Fira Code', 'Consolas', monospace;
  }

  .markdown-content :global(pre) {
    background: var(--bg-secondary, rgba(0,0,0,0.08));
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

  .markdown-content :global(blockquote) {
    margin: 0.5em 0;
    padding: 0.3em 0.8em;
    border-left: 3px solid var(--primary, #4a9eff);
    color: var(--text-secondary, #888);
    background: var(--bg-secondary, rgba(0,0,0,0.04));
    border-radius: 0 4px 4px 0;
  }

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
    background: var(--bg-secondary, rgba(0,0,0,0.06));
    font-weight: 600;
  }

  .markdown-content :global(hr) {
    border: none;
    border-top: 1px solid var(--border, #ddd);
    margin: 0.8em 0;
  }

  .markdown-content :global(strong) { font-weight: 600; }

  .markdown-content :global(img) {
    max-width: 100%;
    border-radius: 4px;
  }
</style>

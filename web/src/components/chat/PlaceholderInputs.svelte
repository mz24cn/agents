<script>
  import { extractPlaceholders, replacePlaceholders } from '../../lib/placeholder.js'
  import { marked } from 'marked'
  import { t } from '../../lib/i18n.svelte.js'

  /**
   * onApply(finalText, mode) — mode: 'system' | 'user'
   * getResolved() — 供外部（顶栏按钮）调用，返回当前填充后的文本
   */
  let { template, onApply } = $props()

  let values = $state({})
  let placeholders = $derived(extractPlaceholders(template?.content ?? ''))

  // 先把占位符替换成唯一标记，再 markdown 渲染，再把标记换回高亮 mark
  let previewHtml = $derived(() => {
    if (!template?.content) return ''
    // Step 1: escape placeholders to a safe token before markdown parsing
    const escaped = template.content.replace(
      /\{\{(\w+)\}\}/g,
      (_, name) => `\x00PH_START\x00${name}\x00PH_END\x00`
    )
    // Step 2: render markdown
    let html = ''
    try {
      html = marked.parse(escaped, { gfm: true, breaks: true })
    } catch {
      html = escaped.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    }
    // Step 3: replace tokens with styled mark elements
    html = html
      .replace(/\x00PH_START\x00/g, '<mark class="ph">')
      .replace(/\x00PH_END\x00/g, '</mark>')
    return html
  })

  export function getResolved() {
    return replacePlaceholders(template?.content ?? '', values)
  }

  export function getValues() {
    return { ...values }
  }
</script>

<div class="placeholder-inputs">
  <div class="preview-section">
    <div class="preview-content">{@html previewHtml()}</div>
  </div>

  {#if placeholders.length > 0}
    <div class="inputs-section">
      {#each placeholders as name}
        <div class="input-group">
          <label for="ph-{name}">{name}</label>
          <input
            id="ph-{name}"
            type="text"
            placeholder={t('inputPlaceholderValue', { name })}
            bind:value={values[name]}
          />
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .placeholder-inputs {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 4px 2px;
  }
  .preview-content { font-size: 0.9rem; color: var(--text); line-height: 1.6; }
  .preview-content :global(p) { margin: 0.4em 0; }
  .preview-content :global(h1), .preview-content :global(h2), .preview-content :global(h3) { margin: 0.8em 0 0.4em; font-weight: 600; }
  .preview-content :global(ul), .preview-content :global(ol) { margin: 0.4em 0; padding-left: 1.5em; }
  .preview-content :global(li) { margin: 0.2em 0; }
  .preview-content :global(code) { background: var(--bg-tertiary, rgba(0,0,0,0.1)); padding: 0.15em 0.35em; border-radius: 3px; font-family: monospace; font-size: 0.88em; }
  .preview-content :global(pre) { background: var(--bg-tertiary, rgba(0,0,0,0.08)); padding: 0.8em 1em; border-radius: 6px; overflow-x: auto; }
  .preview-content :global(pre code) { background: none; padding: 0; }
  .preview-content :global(mark.ph) { background: var(--primary); color: #fff; padding: 1px 4px; border-radius: 3px; font-weight: 600; font-style: normal; }
  .inputs-section { display: flex; flex-direction: column; gap: 8px; }
  .input-group { display: flex; align-items: center; gap: 8px; }
  .input-group label { font-size: 0.85rem; font-weight: 600; color: var(--text); min-width: 100px; }
  .input-group input {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
  }
</style>

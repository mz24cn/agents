<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'
  import MarkdownRenderer from './MarkdownRenderer.svelte'

  let { toolCalls = null, compact = false, toolResultsById = {} } = $props()

  let expandedIndexes = $state(new Set())
  let hoverExpandedIndexes = $state(new Set())
  let resultExpandedIndexes = $state(new Set())
  let resultHoverExpandedIndexes = $state(new Set())
  const hoverTimers = new Map()
  const resultHoverTimers = new Map()

  function getToolCallId(tc) {
    return tc?.id || tc?.tool_use_id || null
  }

  function getToolResult(tc) {
    const id = getToolCallId(tc)
    return id ? toolResultsById[id] || null : null
  }

  function isExpanded(index) {
    return expandedIndexes.has(index) || hoverExpandedIndexes.has(index)
  }

  function isResultExpanded(index) {
    return resultExpandedIndexes.has(index) || resultHoverExpandedIndexes.has(index)
  }

  function startHoverPreview(index) {
    if (expandedIndexes.has(index) || hoverTimers.has(index)) return
    hoverTimers.set(index, setTimeout(() => {
      hoverTimers.delete(index)
      hoverExpandedIndexes = new Set(hoverExpandedIndexes).add(index)
    }, 1000))
  }

  function stopHoverPreview(index) {
    const timer = hoverTimers.get(index)
    if (timer) clearTimeout(timer)
    hoverTimers.delete(index)
    if (hoverExpandedIndexes.has(index)) {
      const next = new Set(hoverExpandedIndexes)
      next.delete(index)
      hoverExpandedIndexes = next
    }
  }

  function startResultHoverPreview(index) {
    if (resultExpandedIndexes.has(index) || resultHoverTimers.has(index)) return
    resultHoverTimers.set(index, setTimeout(() => {
      resultHoverTimers.delete(index)
      resultHoverExpandedIndexes = new Set(resultHoverExpandedIndexes).add(index)
    }, 1000))
  }

  function stopResultHoverPreview(index) {
    const timer = resultHoverTimers.get(index)
    if (timer) clearTimeout(timer)
    resultHoverTimers.delete(index)
    if (resultHoverExpandedIndexes.has(index)) {
      const next = new Set(resultHoverExpandedIndexes)
      next.delete(index)
      resultHoverExpandedIndexes = next
    }
  }

  function toggleExpanded(index) {
    const next = new Set(expandedIndexes)
    if (next.has(index)) next.delete(index)
    else next.add(index)
    expandedIndexes = next
  }

  function toggleResultExpanded(index) {
    const next = new Set(resultExpandedIndexes)
    if (next.has(index)) next.delete(index)
    else next.add(index)
    resultExpandedIndexes = next
  }

  function highlightArgs(args) {
    if (!args) return ''
    try {
      const pretty = JSON.stringify(typeof args === 'string' ? JSON.parse(args) : args, null, 2)
      return highlight(pretty, 'json')
    } catch {
      return highlight(String(args), 'json')
    }
  }

  function resultContent(result) {
    return result?.content ?? ''
  }

  function isToolError(result) {
    const content = String(resultContent(result))
    try {
      const parsed = JSON.parse(content)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && Object.prototype.hasOwnProperty.call(parsed, 'error')) return true
    } catch {}
    return /^Error:/i.test(content.trim())
  }

  function renderResult(result) {
    const content = String(resultContent(result))
    try {
      const parsed = JSON.parse(content)
      return { html: highlight(JSON.stringify(parsed, null, 2), 'json'), lang: 'JSON', markdown: '' }
    } catch {}
    const firstLine = content.split('\n')[0]
    if (firstLine.startsWith('#!')) {
      const lang = /\bpython\b/.test(firstLine) ? 'python' : /\b(bash|sh)\b/.test(firstLine) ? 'bash' : null
      if (lang) return { html: highlight(content, lang), lang: lang.toUpperCase(), markdown: '' }
    }
    return { html: null, lang: '', markdown: content }
  }
</script>

{#if toolCalls && toolCalls.length > 0}
  {#if compact}
    {#each toolCalls as tc, index}
      {@const result = getToolResult(tc)}
      {@const renderedResult = result ? renderResult(result) : null}
      <span class="compact-tool-pair" role="group">
        <span class="compact-tool-zone" role="group" onmouseenter={() => startHoverPreview(index)} onmouseleave={() => stopHoverPreview(index)}>
          <button class="compact-tool-call" class:has-result={!!result} aria-expanded={isExpanded(index)} onclick={() => toggleExpanded(index)}>
            <span class="compact-tc-icon">🛠️</span>
            <span class="compact-tc-name">{tc.name ?? t('unknownTool')}</span>
          </button>
          {#if isExpanded(index)}
            <span class="tool-detail compact-expanded">
              <pre><code>{@html highlightArgs(tc.arguments ?? tc)}</code></pre>
            </span>
          {/if}
        </span>
        {#if result}
          <span class="compact-result-zone" role="group" onmouseenter={() => startResultHoverPreview(index)} onmouseleave={() => stopResultHoverPreview(index)}>
            <button class="compact-tool-result" aria-expanded={isResultExpanded(index)} onclick={() => toggleResultExpanded(index)}>
              {isToolError(result) ? '✖️' : '✔️'}
            </button>
            {#if isResultExpanded(index)}
              <span class="tool-detail result-expanded">
                {#if renderedResult.html}
                  <span class="detail-lang">{renderedResult.lang}</span>
                  <pre><code>{@html renderedResult.html}</code></pre>
                {:else}
                  <MarkdownRenderer content={renderedResult.markdown} />
                {/if}
              </span>
            {/if}
          </span>
        {/if}
      </span>
    {/each}
  {:else}
    {#each toolCalls as tc}
      <div class="tool-call">
        {t('callingTool', { name: tc.name ?? t('unknownTool') })}
        <pre><code>{@html highlightArgs(tc.arguments ?? tc)}</code></pre>
      </div>
    {/each}
  {/if}
{/if}

<style>
  .tool-call {
    font-size: 0.8rem;
    margin-top: 6px;
    padding: 6px 8px;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
  }
  .tool-call pre,
  .tool-detail pre {
    margin: 4px 0 0;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .compact-tool-pair,
  .compact-tool-zone,
  .compact-result-zone {
    display: inline;
  }
  .compact-tool-pair {
    margin: 2px 4px 2px 0;
    /* Remove the HTML inline whitespace between the independently clickable
       call and result zones so they render as one continuous capsule. */
    font-size: 0;
  }
  .compact-tool-call,
  .compact-tool-result {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    height: 24px;
    padding: 0 10px;
    vertical-align: middle;
    font-family: inherit;
    font-size: 0.78rem;
    line-height: 1;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    border: 1px solid transparent;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .compact-tool-call {
    gap: 5px;
    border-radius: 12px;
  }
  .compact-tool-call.has-result {
    padding-right: 4px;
    border-radius: 12px 0 0 12px;
    border-right: none;
  }
  .compact-tool-result {
    padding-left: 3px;
    padding-right: 8px;
    border-radius: 0 12px 12px 0;
    border-left: none;
  }
  .compact-tool-call:hover,
  .compact-tool-result:hover {
    color: var(--text, #333);
    background: var(--bg-secondary, rgba(0,0,0,0.12));
    border-color: var(--border, rgba(128,128,128,0.3));
  }
  .compact-tool-call.has-result:hover {
    border-right: none;
  }
  .compact-tool-result:hover {
    border-left: none;
  }
  .compact-tc-icon,
  .compact-tool-result {
    line-height: 1;
  }
  .compact-tc-icon {
    display: inline-flex;
    align-items: center;
    font-size: 0.85rem;
  }
  .compact-tc-name { font-weight: 500; }

  .tool-detail {
    display: block;
    position: relative;
    margin: 2px 0 4px;
    padding: 6px 8px;
    font-size: 0.8rem;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
  }
  .detail-lang {
    position: absolute;
    right: 0;
    bottom: 0;
    padding: 2px 8px;
    font-size: 0.7em;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border-radius: 4px 0 4px 0;
  }

  .tool-call :global(.hl-key), .tool-detail :global(.hl-key) { color: #82aaff; }
  .tool-call :global(.hl-string), .tool-detail :global(.hl-string) { color: #c3e88d; }
  .tool-call :global(.hl-number), .tool-detail :global(.hl-number) { color: #f78c6c; }
  .tool-call :global(.hl-boolean), .tool-detail :global(.hl-boolean) { color: #ff5874; }
  .tool-call :global(.hl-null), .tool-detail :global(.hl-null) { color: #ff5874; }

  :root[data-theme="light"] .tool-call :global(.hl-key),
  :root[data-theme="light"] .tool-detail :global(.hl-key) { color: #1d4ed8; }
  :root[data-theme="light"] .tool-call :global(.hl-string),
  :root[data-theme="light"] .tool-detail :global(.hl-string) { color: #16a34a; }
  :root[data-theme="light"] .tool-call :global(.hl-number),
  :root[data-theme="light"] .tool-detail :global(.hl-number) { color: #c2410c; }
  :root[data-theme="light"] .tool-call :global(.hl-boolean),
  :root[data-theme="light"] .tool-detail :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .tool-call :global(.hl-null),
  :root[data-theme="light"] .tool-detail :global(.hl-null) { color: #dc2626; }
</style>

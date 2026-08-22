<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'
  import { isToolErrorContent } from '../../lib/tool-result.js'
  import MarkdownRenderer from './MarkdownRenderer.svelte'

  let { toolCalls = null, compact = false, toolResultsById = {} } = $props()

  let expandedIndexes = $state(new Set())
  let hoverExpandedIndexes = $state(new Set())
  let resultExpandedIndexes = $state(new Set())
  let resultHoverExpandedIndexes = $state(new Set())
  const hoverTimers = new Map()
  const resultHoverTimers = new Map()
  let previouslyStreamingIds = new Set()

  $effect(() => {
    const currentlyStreamingIds = new Set()
    for (const tc of toolCalls || []) {
      const id = getToolCallId(tc)
      if (id && isResultStreaming(getToolResult(tc))) currentlyStreamingIds.add(id)
    }

    // A streaming detail is implicitly open.  On completion, remove any click
    // or hover expansion accumulated while it was live so the result closes and
    // returns to the exact same interaction model as every other tool result.
    const completedIndexes = []
    for (let index = 0; index < (toolCalls || []).length; index++) {
      const id = getToolCallId(toolCalls[index])
      if (id && previouslyStreamingIds.has(id) && !currentlyStreamingIds.has(id)) {
        completedIndexes.push(index)
      }
    }
    if (completedIndexes.length) {
      const nextExpanded = new Set(resultExpandedIndexes)
      const nextHover = new Set(resultHoverExpandedIndexes)
      for (const index of completedIndexes) {
        nextExpanded.delete(index)
        nextHover.delete(index)
        const timer = resultHoverTimers.get(index)
        if (timer) clearTimeout(timer)
        resultHoverTimers.delete(index)
      }
      resultExpandedIndexes = nextExpanded
      resultHoverExpandedIndexes = nextHover
    }
    previouslyStreamingIds = currentlyStreamingIds
  })

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

  function isResultStreaming(result) {
    return result?.streaming === true || !!(result?.sub_messages && Object.values(result.sub_messages).some(sm => sm.streaming))
  }

  function isResultExpanded(index, result) {
    // Any tool that supplies streaming result frames reveals them in the normal
    // result detail area.  Today this is used by talk_to/delegate; the same UI
    // path will work for streaming MCP results once their transport emits the
    // shared `streaming` state.
    return isResultStreaming(result) || resultExpandedIndexes.has(index) || resultHoverExpandedIndexes.has(index)
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
    // A pending/active hover preview must not outlive a click toggle. Otherwise
    // the click state closes while the hover state keeps the detail visible.
    const wasClickExpanded = expandedIndexes.has(index)
    stopHoverPreview(index)

    const next = new Set(expandedIndexes)
    if (wasClickExpanded) next.delete(index)
    else next.add(index)
    expandedIndexes = next
  }

  function toggleResultExpanded(index) {
    // Keep result-detail clicks independent from their hover-preview state too.
    const wasClickExpanded = resultExpandedIndexes.has(index)
    stopResultHoverPreview(index)

    const next = new Set(resultExpandedIndexes)
    if (wasClickExpanded) next.delete(index)
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
    if (result?.sub_messages) {
      return Object.values(result.sub_messages)
        .map(sm => {
          const name = sm.agent_nickname || sm.agent_id || ''
          const id = sm.agent_id || ''
          const prefix = id ? `**${name}** (${id}): ` : (name ? `**${name}**: ` : '')
          return prefix + (sm.content || '')
        })
        .join('\n\n')
    }
    return result?.content ?? ''
  }

  function isToolError(result) {
    return isToolErrorContent(resultContent(result))
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
      {@const resultStreaming = isResultStreaming(result)}
      {@const renderedResult = result ? renderResult(result) : null}
      <span class="compact-tool-pair" role="group">
        <span class="compact-tool-zone" role="group" onmouseenter={() => startHoverPreview(index)} onmouseleave={() => stopHoverPreview(index)}>
          <button class="compact-tool-call has-result" aria-expanded={isExpanded(index)} onclick={() => toggleExpanded(index)}>
            <span class="compact-tc-icon">🛠️</span>
            <span class="compact-tc-name">{tc.name ?? t('unknownTool')}</span>
          </button>
          {#if isExpanded(index)}
            <span class="tool-detail compact-expanded">
              <pre><code>{@html highlightArgs(tc.arguments ?? tc)}</code></pre>
            </span>
          {/if}
        </span>
        <span class="compact-result-zone" role="group" onmouseenter={() => result && startResultHoverPreview(index)} onmouseleave={() => stopResultHoverPreview(index)}>
          <button class="compact-tool-result" class:pending={!result || resultStreaming} aria-expanded={isResultExpanded(index, result)} disabled={!result} onclick={() => result && toggleResultExpanded(index)}>
            {!result || resultStreaming ? '⏳' : isToolError(result) ? '✖️' : '✔️'}
          </button>
          {#if result && isResultExpanded(index, result)}
            <span class="tool-detail result-expanded" class:streaming={resultStreaming}>
              {#if renderedResult.html}
                <span class="detail-lang">{renderedResult.lang}</span>
                <pre><code>{@html renderedResult.html}</code></pre>
              {:else if renderedResult.markdown}
                <MarkdownRenderer content={renderedResult.markdown} />
              {:else if resultStreaming}
                <span class="stream-placeholder">⏳</span>
              {/if}
            </span>
          {/if}
        </span>
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

  .compact-tool-zone,
  .compact-result-zone,
  .compact-tool-pair {
    display: inline;
  }
  .compact-tool-pair {
    position: relative;
    /* The badge's 0.78rem text/1.3em control box sits about 6px above the
       surrounding markdown glyph box. Shift the complete call/result pair as
       one unit instead of mixing a pixel offset with baseline alignment. */
    top: 6px;
    margin: 0 4px 0 0;
    vertical-align: baseline;
    /* Remove the HTML inline whitespace between the independently clickable
       call and result zones so they render as one continuous capsule. */
    font-size: 0;
  }
  .compact-tool-zone,
  .compact-result-zone {
    vertical-align: top;
  }
  .compact-tool-call,
  .compact-tool-result {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    height: 1.3em;
    padding: 0 7px;
    vertical-align: top;
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
    padding-right: 3px;
    border-radius: 12px 0 0 12px;
    border-right: none;
  }
  .compact-tool-result {
    padding-left: 2px;
    padding-right: 6px;
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
  .compact-tool-result:disabled {
    cursor: default;
    opacity: 1;
  }
  .compact-tool-result.pending {
    animation: tool-pending-pulse 1.2s ease-in-out infinite;
  }
  .tool-detail.streaming {
    min-height: 1.5em;
  }
  .stream-placeholder {
    display: inline-block;
    animation: tool-pending-pulse 1.2s ease-in-out infinite;
  }
  @keyframes tool-pending-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.45; }
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
    box-sizing: border-box;
    width: 100%;
    margin: 4px 0 6px;
    padding: 6px 8px;
    font-size: 0.8rem;
    line-height: 1.5;
    white-space: normal;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
  }
  /* Force the expanded detail onto its own full-width line in the assistant
     text flow. The zero-width break consumes the remainder of the current
     line, so all following text/tools continue below the detail block. */
  .tool-detail::before {
    content: '';
    display: block;
  }
  .tool-detail::after {
    content: '';
    display: block;
    clear: both;
  }
  .tool-detail :global(.markdown-content) {
    line-height: 1.5;
  }
  .tool-detail pre,
  .tool-detail code {
    line-height: 1.5;
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

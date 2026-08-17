<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'

  let { toolCalls = null, compact = false } = $props()

  let expandedIndexes = $state(new Set())
  let hoverExpandedIndexes = $state(new Set())
  const hoverTimers = new Map()

  function isExpanded(index) {
    return expandedIndexes.has(index) || hoverExpandedIndexes.has(index)
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

  function toggleExpanded(index) {
    const next = new Set(expandedIndexes)
    if (next.has(index)) next.delete(index)
    else next.add(index)
    expandedIndexes = next
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
</script>

{#if toolCalls && toolCalls.length > 0}
  {#if compact}
    <!-- Compact mode: show "🛠️{name}" as clickable badge -->
    {#each toolCalls as tc, index}
      <span class="compact-tool-call-wrap" role="group" onmouseenter={() => startHoverPreview(index)} onmouseleave={() => stopHoverPreview(index)}>
        <button class="compact-tool-call" aria-expanded={isExpanded(index)} onclick={() => toggleExpanded(index)}>
          <span class="compact-tc-icon">🛠️</span>
          <span class="compact-tc-name">{tc.name ?? t('unknownTool')}</span>
        </button>
        {#if isExpanded(index)}
          <span class="tool-call compact-expanded">
            <pre><code>{@html highlightArgs(tc.arguments ?? tc)}</code></pre>
          </span>
        {/if}
      </span>
    {/each}
  {:else}
    <!-- Normal mode -->
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
  .tool-call pre {
    margin: 4px 0 0;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .tool-call.compact-expanded {
    margin-top: 2px;
    margin-bottom: 4px;
  }

  /* Compact mode tool call badge */
  .compact-tool-call-wrap {
    display: inline;
  }
  .compact-tool-call {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 1px 10px;
    font-size: 0.78rem;
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    border: 1px solid transparent;
    border-radius: 12px;
    cursor: pointer;
    color: var(--text-secondary, #888);
    transition: background 0.15s, border-color 0.15s;
    line-height: 1.6;
    font-family: inherit;
    margin: 2px 4px 2px 0;
  }
  .compact-tool-call:hover {
    background: var(--bg-secondary, rgba(0,0,0,0.12));
    border-color: var(--border, rgba(128,128,128,0.3));
    color: var(--text, #333);
  }
  .compact-tc-icon {
    font-size: 0.85rem;
  }
  .compact-tc-name {
    font-weight: 500;
  }

  /* Syntax highlighting - dark theme (default) */
  .tool-call :global(.hl-key)     { color: #82aaff; }
  .tool-call :global(.hl-string)  { color: #c3e88d; }
  .tool-call :global(.hl-number)  { color: #f78c6c; }
  .tool-call :global(.hl-boolean) { color: #ff5874; }
  .tool-call :global(.hl-null)    { color: #ff5874; }

  /* Syntax highlighting - light theme overrides */
  :root[data-theme="light"] .tool-call :global(.hl-key)     { color: #1d4ed8; }
  :root[data-theme="light"] .tool-call :global(.hl-string)  { color: #16a34a; }
  :root[data-theme="light"] .tool-call :global(.hl-number)  { color: #c2410c; }
  :root[data-theme="light"] .tool-call :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .tool-call :global(.hl-null)    { color: #dc2626; }
</style>

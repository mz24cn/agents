<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'

  let { toolCalls = null } = $props()

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
  {#each toolCalls as tc}
    <div class="tool-call">
      {t('callingTool', { name: tc.name ?? t('unknownTool') })}
      <pre><code>{@html highlightArgs(tc.arguments ?? tc)}</code></pre>
    </div>
  {/each}
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

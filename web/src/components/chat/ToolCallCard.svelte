<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { toolCalls = null } = $props()
</script>

{#if toolCalls && toolCalls.length > 0}
  {#each toolCalls as tc}
    <div class="tool-call">
      {t('callingTool', { name: tc.name ?? t('unknownTool') })}
      <pre>{tc.arguments ?? JSON.stringify(tc, null, 2)}</pre>
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
</style>

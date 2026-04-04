<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { thinking = '' } = $props()
  let expanded = $state(false)
</script>

<div class="thinking-block">
  <button class="toggle" onclick={() => expanded = !expanded}>
    💭 {expanded ? t('collapseThinking') : t('expandThinking')}{t('thinkingProcess')}
  </button>
  {#if expanded}
    <div class="thinking-content">{thinking}</div>
  {/if}
</div>

<style>
  .thinking-block {
    margin-bottom: 6px;
  }
  .toggle {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
  }
  .toggle:hover {
    background: var(--bg-secondary);
  }
  .thinking-content {
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
    margin-top: 4px;
    padding: 6px 8px;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
    white-space: pre-wrap;
  }
</style>

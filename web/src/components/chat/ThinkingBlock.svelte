<script>
  let { thinking = '', expanded = true } = $props()
</script>

{#if expanded}
  <div class="thinking-content">{thinking}</div>
{/if}

<style>
  .thinking-content {
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
    margin-top: 4px;
    margin-bottom: 6px;
    padding: 6px 8px;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
    white-space: pre-wrap;
  }
</style>

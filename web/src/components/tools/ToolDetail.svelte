<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { tool, onClose } = $props()
</script>

<div class="tool-detail">
  <div class="detail-header">
    <h3>{t('toolDetail')}</h3>
    <button class="btn btn-close" onclick={onClose}>{t('close')}</button>
  </div>

  <div class="detail-body">
    <div class="detail-row">
      <span class="detail-label">{t('toolId')}</span>
      <span class="detail-value">{tool.tool_id}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">{t('toolName')}</span>
      <span class="detail-value">{tool.name}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">{t('toolType')}</span>
      <span class="detail-value">
        {tool.tool_type}
        {#if tool.builtin}<span class="badge-builtin">{t('builtin')}</span>{/if}
      </span>
    </div>
    <div class="detail-row">
      <span class="detail-label">{t('toolDescription')}</span>
      <span class="detail-value">{tool.description}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Parameters (JSON Schema)</span>
      <pre class="detail-json">{JSON.stringify(tool.parameters, null, 2)}</pre>
    </div>
    {#if tool.tool_type === 'function' && tool.function_file_path}
      <div class="detail-row">
        <span class="detail-label">{t('functionFilePath')}</span>
        <span class="detail-value">{tool.function_file_path}</span>
      </div>
    {/if}
    {#if tool.tool_type === 'function' && tool.function_name}
      <div class="detail-row">
        <span class="detail-label">{t('functionName')}</span>
        <span class="detail-value">{tool.function_name}</span>
      </div>
    {/if}
  </div>
</div>

<style>
  .tool-detail { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  .detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  h3 { margin: 0; color: var(--text); }
  .btn-close { padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg-secondary); color: var(--text); cursor: pointer; font-size: 0.85rem; }
  .btn-close:hover { opacity: 0.8; }
  .badge-builtin { display: inline-block; font-size: 0.7rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; background: #16a34a22; color: #16a34a; margin-left: 6px; }
  .detail-body { display: flex; flex-direction: column; gap: 12px; }
  .detail-row { display: flex; flex-direction: column; gap: 4px; }
  .detail-label { font-size: 0.85rem; color: var(--text-secondary); font-weight: 600; }
  .detail-value { font-size: 0.9rem; color: var(--text); }
  .detail-json { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-family: monospace; font-size: 0.85rem; color: var(--text); overflow-x: auto; margin: 0; white-space: pre-wrap; word-break: break-word; }
</style>

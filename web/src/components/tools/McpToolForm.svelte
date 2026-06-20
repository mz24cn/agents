<script>
  import { tools } from '../../lib/api.js'
  import { refreshTools } from '../../lib/catalog-state.svelte.js'
  import { t } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'

  let { tool, onSuccess, onCancel } = $props()

  let name = $state(tool.name ?? '')
  let description = $state(tool.description ?? '')
  let parametersText = $state(tool.parameters ? JSON.stringify(tool.parameters, null, 2) : '')
  let labelsText = $state((tool.labels ?? []).join(', '))
  let submitError = $state('')
  let submitting = $state(false)

  async function handleSubmit() {
    submitting = true
    submitError = ''
    const parsedLabels = labelsText.trim() ? labelsText.split(',').map(s => s.trim()).filter(Boolean) : []
    try {
      await tools.update(tool.tool_id, {
        tool_type: 'mcp',
        name,
        description,
        parameters: parametersText ? JSON.parse(parametersText) : undefined,
        labels: parsedLabels
      })
      await refreshTools()
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="mcp-tool-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <h3>{t('editMcpTool')}</h3>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="info-section">
    <div class="info-row">
      <span class="info-label">{t('toolId')}:</span>
      <span class="info-value">{tool.tool_id}</span>
    </div>
    <div class="info-row">
      <span class="info-label">{t('mcpServerName')}:</span>
      <span class="info-value">{tool.mcp_server_name ?? '-'}</span>
    </div>
  </div>

  <div class="form-group">
    <label for="tool_name">{t('name')}</label>
    <input id="tool_name" type="text" bind:value={name} />
  </div>

  <div class="form-group">
    <label for="tool_description">{t('description')}</label>
    <textarea id="tool_description" bind:value={description} rows={6}></textarea>
  </div>

  <div class="form-group">
    <label for="tool_parameters">{t('parameters')}</label>
    <JsonEditor id="tool_parameters" bind:value={parametersText} rows={6} />
  </div>

  <div class="form-group">
    <label for="tool_labels">{t('labels')}</label>
    <input id="tool_labels" type="text" bind:value={labelsText} placeholder={t('labelsPlaceholder')} />
    <span class="hint">{t('labelsHint')}</span>
  </div>

  <div class="form-actions">
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : t('save')}
    </button>
  </div>
</form>

<style>
  .mcp-tool-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  h3 { margin: 0 0 16px 0; color: var(--text); }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .info-section { background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-bottom: 16px; }
  .info-row { display: flex; margin-bottom: 8px; font-size: 0.9rem; }
  .info-row:last-child { margin-bottom: 0; }
  .info-label { color: var(--text-secondary); min-width: 120px; flex-shrink: 0; }
  .info-value { color: var(--text); word-break: break-all; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  input, textarea { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; }
  textarea { resize: vertical; }
  .hint { color: var(--text-secondary); font-size: 0.8rem; margin-top: 4px; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
</style>

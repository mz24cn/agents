<script>
  import { promptTemplates } from '../../lib/api.js'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { template = null, onSuccess, onCancel } = $props()

  const isEdit = template !== null
  const _init = template ?? {}

  let templateId = $state(_init.template_id ?? '')
  let content = $state(_init.content ?? '')

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)

  let placeholders = $derived(extractPlaceholders(content))

  function validate() {
    const e = {}
    if (!templateId.trim()) e.templateId = t('templateIdRequired')
    if (!content.trim()) e.content = t('templateContentRequired')
    errors = e
    return Object.keys(e).length === 0
  }

  async function handleSubmit() {
    if (!validate()) return
    submitting = true
    submitError = ''
    const payload = { template_id: templateId.trim(), content: content.trim() }
    try {
      if (isEdit) await promptTemplates.update(template.template_id, payload)
      else await promptTemplates.create(payload)
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="prompt-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <h3>{isEdit ? t('editTemplate') : t('newTemplate')}</h3>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="form-group">
    <label for="tpl_id">{t('templateId')} <span class="required">{t('required')}</span></label>
    <input id="tpl_id" type="text" bind:value={templateId} placeholder={t('templateIdPlaceholder')} />
    {#if errors.templateId}<span class="field-error">{errors.templateId}</span>{/if}
  </div>

  <div class="form-group">
    <label for="tpl_content">{t('templateContent')} <span class="required">{t('required')}</span></label>
    <textarea id="tpl_content" bind:value={content} rows="8" placeholder={t('templateContentPlaceholder')}></textarea>
    {#if errors.content}<span class="field-error">{errors.content}</span>{/if}
  </div>

  {#if placeholders.length > 0}
    <div class="placeholders-preview">
      <span class="placeholders-label">{t('detectedPlaceholders')}</span>
      {#each placeholders as ph}
        <span class="placeholder-tag">{ph}</span>
      {/each}
    </div>
  {/if}

  <div class="form-actions">
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : (isEdit ? t('save') : t('create'))}
    </button>
  </div>
</form>

<style>
  .prompt-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  h3 { margin: 0 0 16px 0; color: var(--text); }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, textarea { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  textarea { resize: vertical; }
  .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
  .placeholders-preview { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; padding: 10px 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; }
  .placeholders-label { color: var(--text-secondary); margin-right: 4px; }
  .placeholder-tag { display: inline-block; padding: 2px 8px; background: var(--primary); color: #fff; border-radius: 4px; font-size: 0.8rem; font-family: monospace; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
</style>

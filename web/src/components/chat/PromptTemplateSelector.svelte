<script>
  import { promptTemplates } from '../../lib/api.js'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { onSelect, applyToSystem = $bindable(false) } = $props()

  let templateList = $state([])
  let loading = $state(true)
  let error = $state('')

  async function fetchTemplates() {
    loading = true
    error = ''
    try {
      const data = await promptTemplates.list()
      templateList = data.templates ?? []
    } catch (err) {
      error = err.message || t('fetchTemplatesFailed')
    } finally {
      loading = false
    }
  }

  function handleChange(e) {
    const templateId = e.target.value
    if (!templateId) { onSelect?.(null); return }
    const template = templateList.find(t => t.template_id === templateId)
    if (!template) return
    const placeholders = extractPlaceholders(template.content)
    if (placeholders.length === 0) {
      onSelect?.({ type: 'direct', content: template.content })
    } else {
      onSelect?.({ type: 'template', template })
    }
  }

  $effect(() => { fetchTemplates() })
</script>

<div class="template-selector">
  <label for="template-select">{t('promptTemplateLabel')}</label>
  {#if loading}
    <span class="hint">{t('loading')}</span>
  {:else if error}
    <span class="hint error">{error}</span>
  {:else}
    <select id="template-select" onchange={handleChange}>
      <option value="">{t('selectTemplatePlaceholder')}</option>
      {#each templateList as tpl (tpl.template_id)}
        <option value={tpl.template_id}>{tpl.name}</option>
      {/each}
    </select>
  {/if}
  <label class="checkbox-label">
    <input type="checkbox" bind:checked={applyToSystem} />
    {t('applyToSystem')}
  </label>
</div>

<style>
  .template-selector { display: flex; align-items: center; gap: 8px; }
  label { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
  select {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.9rem;
    min-width: 180px;
  }
  .hint { font-size: 0.8rem; color: var(--text-secondary); }
  .hint.error { color: var(--danger); }
  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-secondary);
    cursor: pointer;
    white-space: nowrap;
  }
  .checkbox-label input[type="checkbox"] { cursor: pointer; }
</style>

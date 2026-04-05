<script>
  import { extractPlaceholders, replacePlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { template, onApply } = $props()

  let values = $state({})
  let placeholders = $derived(extractPlaceholders(template?.content ?? ''))

  let previewHtml = $derived(() => {
    if (!template?.content) return ''
    return template.content.replace(
      /\{\{(\w+)\}\}/g,
      (_, name) => `<mark class="ph">${name}</mark>`
    )
  })

  function handleSubmit() {
    const finalText = replacePlaceholders(template.content, values)
    onApply?.(finalText)
  }
</script>

<div class="placeholder-inputs">
  <div class="preview-section">
    <span class="preview-label">{t('templatePreview')}</span>
    <div class="preview-content">{@html previewHtml()}</div>
  </div>

  <div class="inputs-section">
    {#each placeholders as name}
      <div class="input-group">
        <label for="ph-{name}">{name}</label>
        <input
          id="ph-{name}"
          type="text"
          placeholder={t('inputPlaceholderValue', { name })}
          bind:value={values[name]}
        />
      </div>
    {/each}
  </div>

  <button class="btn btn-primary" onclick={handleSubmit}>{t('applyTemplate')}</button>
</div>

<style>
  .placeholder-inputs {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .preview-label { font-size: 0.8rem; font-weight: 600; color: var(--text-secondary); }
  .preview-content { margin-top: 4px; font-size: 0.85rem; color: var(--text); white-space: pre-wrap; line-height: 1.5; }
  .preview-content :global(.ph) { background: var(--primary); color: #fff; padding: 1px 4px; border-radius: 3px; font-weight: 600; }
  .inputs-section { display: flex; flex-direction: column; gap: 8px; }
  .input-group { display: flex; align-items: center; gap: 8px; }
  .input-group label { font-size: 0.85rem; font-weight: 600; color: var(--text); min-width: 100px; }
  .input-group input {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
  }
  .btn { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.85rem; align-self: flex-end; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
</style>

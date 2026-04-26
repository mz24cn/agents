<script>
  import { models } from '../../lib/api.js'
  import { t } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'

  let { model = null, onSuccess, onCancel } = $props()

  const _init = model ?? {}
  const isEdit = model !== null
  const originalModelId = _init.model_id ?? ''  // 保存原始ID用于API调用

  let model_id = $state(_init.model_id ?? '')
  let api_base = $state(_init.api_base ?? '')
  let model_name = $state(_init.model_name ?? '')
  let api_key = $state(_init.api_key ?? '')
  let model_type = $state(_init.model_type ?? 'llm')
  let api_protocol = $state(_init.api_protocol ?? 'openai')
  let generate_params_text = $state(
    _init.generate_params ? JSON.stringify(_init.generate_params, null, 2) : ''
  )

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)

  function validate() {
    const e = {}
    if (!model_id.trim()) e.model_id = t('modelIdRequired')
    if (!api_base.trim()) e.api_base = t('apiBaseRequired')
    if (!model_name.trim()) e.model_name = t('modelNameRequired')
    if (generate_params_text.trim()) {
      try { JSON.parse(generate_params_text) } catch { e.generate_params = t('jsonInvalid') }
    }
    errors = e
    return Object.keys(e).length === 0
  }

  async function handleSubmit() {
    if (!validate()) return
    submitting = true
    submitError = ''
    const config = {
      model_id: model_id.trim(),
      api_base: api_base.trim(),
      model_name: model_name.trim(),
      api_key: api_key.trim(),
      model_type,
      api_protocol,
      generate_params: generate_params_text.trim() ? JSON.parse(generate_params_text) : {},
    }
    try {
      if (isEdit) await models.update(originalModelId, config)
      else await models.create(config)
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="model-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <h3>{isEdit ? t('editModel') : t('registerModel')}</h3>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="form-group">
    <label for="model_id">{t('modelId')} <span class="required">{t('required')}</span></label>
    <input id="model_id" type="text" bind:value={model_id} placeholder={t('modelIdPlaceholder')} />
    {#if errors.model_id}<span class="field-error">{errors.model_id}</span>{/if}
  </div>

  <div class="form-group">
    <label for="model_name">{t('modelName')} <span class="required">{t('required')}</span></label>
    <input id="model_name" type="text" bind:value={model_name} placeholder={t('modelNamePlaceholder')} />
    {#if errors.model_name}<span class="field-error">{errors.model_name}</span>{/if}
  </div>

  <div class="form-group">
    <label for="api_base">{t('apiBase')} <span class="required">{t('required')}</span></label>
    <input id="api_base" type="text" bind:value={api_base} placeholder={t('apiBasePlaceholder')} />
    {#if errors.api_base}<span class="field-error">{errors.api_base}</span>{/if}
  </div>

  <div class="form-group">
    <label for="api_key">{t('apiKey')}</label>
    <input id="api_key" type="password" bind:value={api_key} placeholder={t('apiKeyPlaceholder')} />
  </div>

  <div class="form-row">
    <div class="form-group">
      <span class="radio-label">{t('modelType')}</span>
      <div class="radio-group">
        {#each [['llm','LLM'],['vlm','VLM']] as [val, label]}
          <label class="radio-item">
            <input type="radio" name="model_type" value={val} bind:group={model_type} />
            {label}
          </label>
        {/each}
      </div>
    </div>
    <div class="form-group">
      <span class="radio-label">{t('apiProtocol')}</span>
      <div class="radio-group">
        {#each [['openai','OpenAI'],['ollama','Ollama']] as [val, label]}
          <label class="radio-item">
            <input type="radio" name="api_protocol" value={val} bind:group={api_protocol} />
            {label}
          </label>
        {/each}
      </div>
    </div>
  </div>

  <div class="form-group">
    <label for="generate_params">{t('generateParams')}</label>
    <JsonEditor id="generate_params" bind:value={generate_params_text} rows={4} placeholder={t('generateParamsPlaceholder')} />
    {#if errors.generate_params}<span class="field-error">{errors.generate_params}</span>{/if}
  </div>

  <div class="form-actions">
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : (isEdit ? t('save') : t('register'))}
    </button>
  </div>
</form>

<style>
  .model-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  h3 { margin: 0 0 16px 0; color: var(--text); }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  .form-row { display: flex; gap: 16px; }
  .form-row .form-group { flex: 1; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, select, textarea { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  input:disabled { opacity: 0.6; cursor: not-allowed; }
  .radio-label { font-size: 0.9rem; color: var(--text-secondary); margin-bottom: 6px; }
  .radio-group { display: flex; gap: 20px; }
  .radio-item { display: flex; align-items: center; gap: 6px; font-size: 0.9rem; color: var(--text); cursor: pointer; }
  .radio-item input[type="radio"] { padding: 0; border: none; background: none; width: auto; cursor: pointer; }
  textarea { resize: vertical; font-family: monospace; }
  .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
</style>

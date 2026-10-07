<script>
  import { models, apiFetch } from '../../lib/api.js'
  import { refreshModels } from '../../lib/catalog-state.svelte.js'
  import { t, i18n } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { parseLabels } from '../../lib/labels.js'
  import { formatMaxContext, parseMaxContext } from '../../lib/max-context.js'
  import { markProbeRows, pickProbeIndex, shouldFillProbeValue,
           probeColumns, filterProbeRows, probeFeatureText } from '../../lib/model-probe.js'

  let { model = null, onSuccess, onCancel } = $props()

  const _init = model ?? {}
  const originalModelId = _init.model_id ?? ''  // 保存原始ID用于API调用
  const isEdit = originalModelId !== ''  // 有原始ID才是编辑模式，复制时model_id为空算创建

  let model_id = $state(_init.model_id ?? '')
  let api_base = $state(_init.api_base ?? '')
  let model_name = $state(_init.model_name ?? '')
  let api_key = $state(_init.api_key ?? '')
  let showApiKey = $state(false)
  let api_protocol = $state(_init.api_protocol ?? 'openai')
  let generate_params_text = $state(
    _init.generate_params ? JSON.stringify(_init.generate_params, null, 2) : ''
  )
  let labelsText = $state((_init.labels ?? []).join(', '))
  let max_context_text = $state(formatMaxContext(_init.max_context ?? 0))

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)
  let testing = $state(false)
  let testDialogOpen = $state(false)
  let testDialogTitle = $state('')
  let testDialogMessage = $state('')

  // "模型检查": the endpoint's own model list, fetched by the backend
  // (GET /v1/models/probe?api_base=...) and shown as a table under the input.
  let probing = $state(false)
  let probeRows = $state([])
  let probeUrl = $state('')
  let probeError = $state('')
  let probeNote = $state('')
  // 有的网关一次回 200 个模型，所以表格带一个名称过滤框。
  let probeFilter = $state('')

  let apiBasePlaceholder = $derived(
    api_protocol === 'anthropic'
      ? 'https://api.anthropic.com/v1'
      : api_protocol === 'ollama'
        ? 'http://localhost:11434'
        : 'https://api.openai.com/v1'
  )
  let apiBaseHint = $derived(
    api_protocol === 'anthropic'
      ? 'Anthropic API 地址'
      : api_protocol === 'ollama'
        ? 'Ollama 本地地址'
        : api_protocol === 'responses'
          ? t('responsesApiBaseHint')
          : 'OpenAI API 地址'
  )

  function validate() {
    const e = {}
    if (!model_id.trim()) e.model_id = t('modelIdRequired')
    if (!api_base.trim()) e.api_base = t('apiBaseRequired')
    if (!model_name.trim()) e.model_name = t('modelNameRequired')
    if (generate_params_text.trim()) {
      try { JSON.parse(generate_params_text) } catch { e.generate_params = t('jsonInvalid') }
    }
    if (parseMaxContext(max_context_text) === null) e.max_context = t('maxContextInvalid')
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
      api_protocol,
      generate_params: generate_params_text.trim() ? JSON.parse(generate_params_text) : {},
      labels: parseLabels(labelsText),
      max_context: parseMaxContext(max_context_text),
    }
    try {
      if (isEdit) await models.update(originalModelId, config)
      else await models.create(config)
      await refreshModels()
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }

  async function handleTest() {
    if (!validate()) return
    testing = true
    try {
      const userMessage = i18n.lang === 'zh'
        ? '你好！请简单介绍你自己。'
        : 'Hello! Please simply introduce yourself.'

      const body = {
        model_id: model_id.trim(),
        model: {
          model_id: model_id.trim(),
          api_base: api_base.trim(),
          model_name: model_name.trim(),
          api_key: api_key.trim(),
          api_protocol,
          generate_params: generate_params_text.trim() ? JSON.parse(generate_params_text) : {},
          max_context: parseMaxContext(max_context_text),
        },
        messages: [{ role: 'user', content: userMessage }],
        stream: false,
      }

      const res = await apiFetch('/v1/infer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      const data = await res.json()

      if (!res.ok || !data.success) {
        const errorMsg = data.error || data.message || `Request failed: ${res.status}`
        testDialogTitle = t('testFailedTitle')
        testDialogMessage = errorMsg
      } else {
        // Extract assistant content from messages
        const assistantMsg = data.messages?.find(m => m.role === 'assistant')
        testDialogTitle = t('testSuccessTitle')
        testDialogMessage = assistantMsg?.content || ''
      }
      testDialogOpen = true
    } catch (err) {
      testDialogTitle = t('testFailedTitle')
      testDialogMessage = err.message || 'Network error'
      testDialogOpen = true
    } finally {
      testing = false
    }
  }

  function closeTestDialog() {
    testDialogOpen = false
    testDialogTitle = ''
    testDialogMessage = ''
  }

  /** Mark *row* selected and copy its context window into the input.
   *  @param {boolean} onlyIfEmpty - keep a value the user already typed. */
  function applyProbeRow(row, { onlyIfEmpty = false } = {}) {
    if (!row) return
    probeRows = probeRows.map(item => ({ ...item, selected: item === row }))
    const value = Number(row.max_context) || 0
    if (value <= 0) {
      probeNote = t('probeNoContext', { name: row.model_name })
      return
    }
    const formatted = formatMaxContext(value)
    if (onlyIfEmpty && !shouldFillProbeValue(max_context_text)) {
      probeNote = t('probeKept', { name: row.model_name, value: formatted })
      return
    }
    max_context_text = formatted
    if (errors.max_context) errors.max_context = ''
    probeNote = t('probeFilled', { name: row.model_name, value: formatted })
  }

  async function handleProbe() {
    const base = api_base.trim()
    probeError = ''
    probeNote = ''
    probeRows = []
    probeUrl = ''
    probeFilter = ''
    if (!base) {
      probeError = t('probeApiBaseRequired')
      return
    }
    probing = true
    try {
      const data = await models.probe(base, api_key.trim())
      const list = Array.isArray(data?.models) ? data.models : []
      probeUrl = data?.models_url || ''
      if (!list.length) {
        probeError = t('probeEmpty')
        return
      }
      // A single model on the endpoint can only be the one being configured;
      // with several, the form's model_name decides which row is the current one.
      const index = pickProbeIndex(list, model_name)
      probeRows = markProbeRows(list, index)
      if (index < 0) {
        probeNote = t('probeNoMatch', { count: list.length })
        return
      }
      applyProbeRow(probeRows[index], { onlyIfEmpty: true })
    } catch (err) {
      probeError = err?.message || t('operationFailed')
    } finally {
      probing = false
    }
  }
</script>

<form class="model-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <div class="form-header">
    <h3>{isEdit ? t('editModel') : t('registerModel')}</h3>
    <button type="button" class="btn btn-back" onclick={onCancel} disabled={submitting} title={t('cancel')}>&larr; {t('cancel')}</button>
  </div>

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
    <span class="field-hint">{t('modelEnvPlaceholderHint')}</span>
  </div>

  <div class="form-group">
    <label for="api_base">{t('apiBase')} <span class="required">{t('required')}</span></label>
    <input id="api_base" type="text" bind:value={api_base} placeholder={apiBasePlaceholder} />
    {#if errors.api_base}<span class="field-error">{errors.api_base}</span>{/if}
    {#if apiBaseHint}<span class="field-hint">{apiBaseHint}</span>{/if}
    <span class="field-hint">{t('modelEnvPlaceholderHint')}</span>
  </div>

  <div class="form-group">
    <label for="api_key">{t('apiKey')}</label>
    <div class="secret-input">
      <input
        id="api_key"
        type={showApiKey ? 'text' : 'password'}
        bind:value={api_key}
        placeholder={t('apiKeyPlaceholder')}
      />
      <button
        type="button"
        class="secret-toggle"
        onclick={() => { showApiKey = !showApiKey }}
        title={showApiKey ? t('hideApiKey') : t('showApiKey')}
        aria-label={showApiKey ? t('hideApiKey') : t('showApiKey')}
        aria-pressed={showApiKey}
      >
        {#if showApiKey}
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 3l18 18M10.6 10.7a2 2 0 0 0 2.7 2.7M9.9 4.2A10.7 10.7 0 0 1 12 4c5.5 0 9 5.5 9 5.5a16.6 16.6 0 0 1-3 3.6M6.6 6.6A17.3 17.3 0 0 0 3 9.5S6.5 15 12 15a9.8 9.8 0 0 0 3.4-.6" />
          </svg>
        {:else}
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 12s3.5-5.5 9-5.5 9 5.5 9 5.5-3.5 5.5-9 5.5S3 12 3 12Z" />
            <circle cx="12" cy="12" r="2.5" />
          </svg>
        {/if}
      </button>
    </div>
    <span class="field-hint">{t('modelEnvPlaceholderHint')}</span>
  </div>

  <div class="form-group">
    <span class="radio-label">{t('apiProtocol')}</span>
    <div class="radio-group">
      {#each [['openai','OpenAI Chat Completions'],['responses','OpenAI Responses'],['ollama','Ollama'],['anthropic','Anthropic']] as [val, label]}
        <label class="radio-item">
          <input type="radio" name="api_protocol" value={val} bind:group={api_protocol} />
          {label}
        </label>
      {/each}
    </div>
    {#if api_protocol === 'responses'}
      <span class="field-hint">{t('responsesProtocolHint')}</span>
    {/if}
  </div>

  <div class="form-group">
    <label for="generate_params">{t('generateParams')}</label>
    <JsonEditor id="generate_params" bind:value={generate_params_text} rows={4} placeholder={t('generateParamsPlaceholder')} />
    {#if errors.generate_params}<span class="field-error">{errors.generate_params}</span>{/if}
    <span class="field-hint">{t('generateParamsExtraBodyHint')}</span>
  </div>

  <div class="form-group">
    <label for="max_context">{t('maxContext')}</label>
    <div class="field-row">
      <input id="max_context" type="text" bind:value={max_context_text} placeholder={t('maxContextPlaceholder')} />
      <button type="button" class="btn btn-probe" onclick={handleProbe} disabled={probing}
              title={t('probeModelsHint')}>
        {probing ? t('probeLoading') : t('probeModels')}
      </button>
    </div>
    {#if errors.max_context}<span class="field-error">{errors.max_context}</span>{/if}
    <span class="field-hint">{t('maxContextHint')}</span>

    {#if probeError}
      <div class="probe-error">{probeError}</div>
    {:else if probeNote}
      <div class="probe-note">{probeNote}</div>
    {/if}

    {#if probeRows.length}
      {#if probeRows.length > 8}
        <input class="probe-filter" type="search" bind:value={probeFilter}
               placeholder={t('probeFilterPlaceholder')} />
      {/if}
      {@const visible = filterProbeRows(probeRows, probeFilter)}
      {@const cols = probeColumns(probeRows)}
      <div class="probe-table-wrap">
        <table class="probe-table">
          <thead>
            <tr>
              <th>{t('probeColModel')}</th>
              <th class="num" title={t('probeColContextHint')}>{t('probeColContext')}</th>
              {#if cols.output}
                <th class="num" title={t('probeColMaxOutputHint')}>{t('probeColMaxOutput')}</th>
              {/if}
              <th>{t('probeColInput')}</th>
              <th>{t('probeColOutput')}</th>
              {#if cols.features}<th title={t('probeColFeaturesHint')}>{t('probeColFeatures')}</th>{/if}
              {#if cols.status}<th>{t('probeColStatus')}</th>{/if}
            </tr>
          </thead>
          <tbody>
            {#each visible as row}
              <tr class:selected={row.selected} tabindex="0"
                  aria-label={t('probeRowHint')} aria-selected={row.selected}
                  title={t('probeRowHint')}
                  onclick={() => applyProbeRow(row)}
                  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); applyProbeRow(row) } }}>
                <td class="cell-name" title={row.model_name}>
                  <div>{row.model_name}</div>
                  {#if row.details}<div class="cell-sub">{row.details}</div>{/if}
                </td>
                <td class="num" title={row.max_context_source || ''}>
                  {row.max_context ? formatMaxContext(row.max_context) : t('probeUnknown')}
                </td>
                {#if cols.output}
                  <td class="num">{row.max_output ? formatMaxContext(row.max_output) : '—'}</td>
                {/if}
                <td>{row.input_modalities?.length ? row.input_modalities.join(', ') : '—'}</td>
                <td>{row.output_modalities?.length ? row.output_modalities.join(', ') : '—'}</td>
                {#if cols.features}<td>{probeFeatureText(row) || '—'}</td>{/if}
                {#if cols.status}<td>{row.status || '—'}</td>{/if}
              </tr>
            {:else}
              <tr class="probe-no-match"><td colspan="7">{t('probeNoMatchRows')}</td></tr>
            {/each}
          </tbody>
        </table>
        <div class="probe-foot">
          {#if probeUrl}<span class="probe-source">{probeUrl}</span>{/if}
          {#if probeFilter}
            <span class="probe-count">{t('probeCount', { shown: visible.length, total: probeRows.length })}</span>
          {/if}
        </div>
      </div>
    {/if}
  </div>

  <div class="form-group">
    <label for="model_labels">{t('labels')}</label>
    <input id="model_labels" type="text" bind:value={labelsText} placeholder={t('labelsPlaceholder')} />
    <span class="field-hint">{t('labelsModelHint')}</span>
  </div>

  <div class="form-actions">
    <button type="button" class="btn btn-test" onclick={handleTest} disabled={testing}>{testing ? t('submitting') : t('testModel')}</button>
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : (isEdit ? t('save') : t('register'))}
    </button>
  </div>

  <ConfirmDialog
    open={testDialogOpen}
    title={testDialogTitle}
    message={testDialogMessage}
    closeText={t('testClose')}
    hideCancel={true}
    onConfirm={closeTestDialog}
    onCancel={closeTestDialog}
  />
</form>

<style>
.model-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  .form-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .form-header h3 { margin: 0; color: var(--text); }
  .btn-back { background: transparent; color: var(--text-secondary); border: 1px solid var(--border); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; transition: all 0.15s; }
  .btn-back:hover:not(:disabled) { background: var(--border); color: var(--text); }
  .btn-back:disabled { opacity: 0.5; cursor: not-allowed; }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  .form-row { display: flex; gap: 16px; }
  .form-row .form-group { flex: 1; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, select, textarea { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  input:disabled { opacity: 0.6; cursor: not-allowed; }
  .secret-input { position: relative; display: flex; }
  .secret-input input { width: 100%; box-sizing: border-box; padding-right: 42px; }
  .secret-toggle { position: absolute; top: 50%; right: 5px; width: 32px; height: 32px; padding: 6px; transform: translateY(-50%); display: flex; align-items: center; justify-content: center; border: 0; border-radius: 5px; background: transparent; color: var(--text-secondary); cursor: pointer; }
  .secret-toggle:hover { background: var(--border); color: var(--text); }
  .secret-toggle:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
  .secret-toggle svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .radio-label { font-size: 0.9rem; color: var(--text-secondary); margin-bottom: 6px; }
  .radio-group { display: flex; gap: 20px; }
  .radio-item { display: flex; align-items: center; gap: 6px; font-size: 0.9rem; color: var(--text); cursor: pointer; }
  .radio-item input[type="radio"] { padding: 0; border: none; background: none; width: auto; cursor: pointer; }
  textarea { resize: vertical; font-family: monospace; }
   .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
   .field-hint { color: var(--text-secondary); font-size: 0.8rem; margin-top: 2px; font-style: italic; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
   .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
   .btn-test { background: #e67e22; color: #fff; }
   .btn-test:hover:not(:disabled) { background: #d35400; }
   .btn-primary { background: var(--primary); color: #fff; }
   .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  /* max context: input + "模型检查" on one row, probe result table below */
  .field-row { display: flex; gap: 8px; align-items: stretch; }
  .field-row input { flex: 1; min-width: 0; }
  .btn-probe { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); padding: 8px 14px; white-space: nowrap; }
  .btn-probe:hover:not(:disabled) { background: var(--border); }
  .probe-error { color: var(--danger); font-size: 0.8rem; margin-top: 4px; }
  .probe-note { color: var(--text-secondary); font-size: 0.8rem; margin-top: 4px; }
  .probe-filter { margin-top: 8px; width: 240px; max-width: 100%; }
  .probe-table-wrap { margin-top: 8px; border: 1px solid var(--border); border-radius: 6px; overflow-x: auto; }
  .probe-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  .probe-table th, .probe-table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  .probe-table th { background: var(--bg-secondary); color: var(--text-secondary); font-weight: 600; }
  .probe-table tbody tr:last-child td { border-bottom: none; }
  .probe-table tbody tr { cursor: pointer; }
  .probe-table tbody tr:hover { background: var(--bg-secondary); }
  .probe-table tbody tr:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
  .probe-table tbody tr.selected { background: color-mix(in srgb, var(--primary) 14%, var(--bg-secondary)); }
  .probe-table tr.probe-no-match, .probe-table tr.probe-no-match:hover { cursor: default; background: transparent; }
  .probe-table td.cell-name { max-width: 300px; }
  .probe-table td.cell-name > div { overflow: hidden; text-overflow: ellipsis; }
  .probe-table td.cell-name .cell-sub { color: var(--text-secondary); font-size: 0.72rem; }
  .probe-table th.num, .probe-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .probe-foot { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .probe-source { display: block; padding: 4px 10px 6px; color: var(--text-secondary); font-size: 0.75rem; font-family: monospace; }
  .probe-count { padding: 4px 10px 6px; color: var(--text-secondary); font-size: 0.75rem; }
</style>

<script>
  import { agents, models, promptTemplates } from '../../lib/api.js'
  import { t } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'
  import ToolSelector from '../chat/ToolSelector.svelte'

  let { agent = null, onSuccess, onCancel } = $props()

  const isEdit = agent !== null
  const _init = agent ?? {}

  let nickname = $state(_init.nickname ?? '')
  let modelId = $state(_init.model_id ?? '')
  let selectedToolIds = $state((_init.tool_ids ?? []).slice())
  let templateId = $state(_init.template_id ?? '')
  let templateArguments = $state(JSON.stringify(_init.template_arguments ?? {}, null, 2))
  let systemPrompt = $state(_init.system_prompt ?? '')
  let myselfView = $state(_init.myself_view ?? '')
  let description = $state(_init.description ?? '')
  let avatar = $state(_init.avatar ?? '')

  const isTemplateSelected = $derived(templateId.trim() !== '')

  let modelList = $state([])
  let templateList = $state([])
  let loadingMeta = $state(true)

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)

  async function fetchMeta() {
    loadingMeta = true
    try {
      const [modelsData, tplData] = await Promise.all([models.list(), promptTemplates.list()])
      modelList = modelsData.models ?? []
      templateList = tplData.templates ?? []
    } catch {
      // fallback: empty lists
    } finally {
      loadingMeta = false
    }
  }

  $effect(() => { fetchMeta() })

  function validate() {
    const e = {}
    if (!nickname.trim()) e.nickname = t('agentNicknameRequired')
    if (!modelId.trim()) e.modelId = t('agentModelIdRequired')
    if (templateArguments.trim()) {
      try {
        JSON.parse(templateArguments)
      } catch {
        e.templateArgs = t('jsonInvalid')
      }
    }
    errors = e
    return Object.keys(e).length === 0
  }

  async function handleSubmit() {
    if (!validate()) return
    submitting = true
    submitError = ''
    let parsedArgs = {}
    if (templateArguments.trim()) {
      try { parsedArgs = JSON.parse(templateArguments) } catch { parsedArgs = {} }
    }
    const payload = {
      nickname: nickname.trim(),
      model_id: modelId.trim(),
      tool_ids: selectedToolIds,
      template_id: templateId.trim() || null,
      template_arguments: parsedArgs,
      system_prompt: systemPrompt.trim(),
      myself_view: myselfView.trim(),
      description: description.trim(),
      avatar: avatar.trim(),
    }
    try {
      if (isEdit) await agents.update(agent.agent_id, payload)
      else await agents.create(payload)
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="agent-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <h3>{isEdit ? t('editAgent') : t('createAgent')}</h3>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="form-group">
    <label for="agent_nickname">{t('agentNickname')} <span class="required">{t('required')}</span></label>
    <input id="agent_nickname" type="text" bind:value={nickname} placeholder={t('agentNicknamePlaceholder')} />
    {#if errors.nickname}<span class="field-error">{errors.nickname}</span>{/if}
  </div>

  <div class="form-group">
    <label for="agent_model">{t('agentModelId')} <span class="required">{t('required')}</span></label>
    {#if loadingMeta}
      <span class="hint">{t('loading')}</span>
    {:else}
      <select id="agent_model" bind:value={modelId}>
        <option value="">{t('agentModelSelectHint')}</option>
        {#each modelList as m (m.model_id)}
          <option value={m.model_id}>{m.model_name} ({m.model_id})</option>
        {/each}
      </select>
    {/if}
    {#if errors.modelId}<span class="field-error">{errors.modelId}</span>{/if}
  </div>

  <div class="form-group">
    <label>{t('tools')}</label>
    <ToolSelector bind:selectedToolIds />
  </div>

  <div class="form-group">
    <label for="agent_template">{t('agentTemplateId')}</label>
    {#if loadingMeta}
      <span class="hint">{t('loading')}</span>
    {:else}
      <select id="agent_template" bind:value={templateId}>
        <option value="">—</option>
        {#each templateList as tpl (tpl.template_id)}
          <option value={tpl.template_id}>{tpl.template_id}</option>
        {/each}
      </select>
    {/if}
  </div>

  <div class="form-group">
    <label for="agent_template_args">{t('agentTemplateArgs')}</label>
    <JsonEditor id="agent_template_args" bind:value={templateArguments} rows={3} placeholder={"{}"} disabled={!isTemplateSelected} />
    {#if errors.templateArgs}<span class="field-error">{errors.templateArgs}</span>{/if}
  </div>

  <div class="form-group">
    <label for="agent_system_prompt">{t('agentSystemPrompt')}</label>
    <textarea id="agent_system_prompt" bind:value={systemPrompt} rows="4" placeholder={t('agentSystemPromptPlaceholder')} disabled={isTemplateSelected}></textarea>
  </div>

  <div class="form-group">
    <label for="agent_myself">{t('agentMyselfView')}</label>
    <input id="agent_myself" type="text" bind:value={myselfView} placeholder={t('agentMyselfViewPlaceholder')} />
  </div>

  <div class="form-group">
    <label for="agent_desc">{t('agentDescription')}</label>
    <textarea id="agent_desc" bind:value={description} rows="2" placeholder={t('agentDescriptionPlaceholder')}></textarea>
  </div>

  <div class="form-group">
    <label for="agent_avatar">{t('agentAvatar')}</label>
    <input id="agent_avatar" type="text" bind:value={avatar} placeholder={t('agentAvatarPlaceholder')} />
  </div>

  <div class="form-actions">
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : (isEdit ? t('save') : t('create'))}
    </button>
  </div>
</form>

<style>
  .agent-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  h3 { margin: 0 0 16px 0; color: var(--text); }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, textarea, select { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  textarea { resize: vertical; }
  .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
  .hint { font-size: 0.85rem; color: var(--text-secondary); }
  .json-editor:has(textarea:disabled) { opacity: 0.6; }
  .json-editor:has(textarea:disabled)::after { content: ''; position: absolute; inset: 0; cursor: not-allowed; }
  input:disabled, textarea:disabled, select:disabled { opacity: 0.6; cursor: not-allowed; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
</style>

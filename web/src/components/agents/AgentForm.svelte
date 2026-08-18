<script>
  import { agents } from '../../lib/api.js'
  import { catalog, loadPromptTemplates, refreshAgents, loadModels } from '../../lib/catalog-state.svelte.js'
  import { t } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'
  import ToolSelector from '../chat/ToolSelector.svelte'
  import PromptTemplateSelector from '../chat/PromptTemplateSelector.svelte'
  import { parseLabels } from '../../lib/labels.js'

  let { agent = null, onSuccess, onCancel } = $props()

  const _init = agent ?? {}
  const originalAgentId = _init.agent_id ?? ''  // 保存原始ID用于API调用
  const isEdit = originalAgentId !== ''  // 有原始ID才是编辑模式，复制时agent_id为空算创建

  let agentId = $state(_init.agent_id ?? '')
  let nickname = $state(_init.nickname ?? '')
  let modelId = $state(_init.model_id ?? '')
  let selectedToolIds = $state((_init.tool_ids ?? []).slice())
  let templateId = $state(_init.template_id ?? '')
  let templateArguments = $state(JSON.stringify(_init.template_arguments ?? {}, null, 2))
  let systemPrompt = $state(_init.system_prompt ?? '')
  let myselfView = $state(_init.myself_view ?? '')
  let description = $state(_init.description ?? '')
  let labelsText = $state((_init.labels ?? (_init.group ? [_init.group] : [])).join(', '))
  let avatar = $state(_init.avatar ?? '')

  // 模型辅助选择器
  let modelPickerValue = $state('')
  let modelList = $derived(catalog.models.items)
  let modelsLoading = $derived(catalog.models.loading && !catalog.models.loaded)

  // 按第一项标签分组
  let groupedModels = $derived.by(() => {
    const groups = new Map()
    const noTagItems = []
    for (const m of modelList) {
      const firstTag = (m.labels && m.labels.length > 0) ? m.labels[0] : ''
      if (!firstTag) {
        noTagItems.push(m)
      } else {
        if (!groups.has(firstTag)) groups.set(firstTag, [])
        groups.get(firstTag).push(m)
      }
    }
    const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    return { tagged: sortedGroups, untagged: noTagItems }
  })

  function onModelPickerChange(e) {
    const val = e.target.value
    modelPickerValue = val
    if (val) modelId = val
  }

  // templateId 可能被 bind 为 null（PromptTemplateSelector toggle 取消时置 null），必须做 null 安全
  const isTemplateSelected = $derived(!!templateId && templateId.trim() !== '')

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)

  $effect(() => { loadPromptTemplates().catch(() => {}) })
  $effect(() => { loadModels().catch(() => {}) })

  function validateNickname(val = nickname) {
    if (/\s/.test(val)) return t('agentNicknameNoSpaces')
    if (!val.trim()) return t('agentNicknameRequired')
    return ''
  }

  function validateAgentId(val = agentId) {
    if (val.includes('-')) return t('agentIdNoHyphen')
    return ''
  }

  function onAgentIdInput(e) {
    const msg = validateAgentId(e.target.value)
    if (msg) {
      errors = { ...errors, agentId: msg }
    } else if (errors.agentId) {
      const { agentId: _omit, ...rest } = errors
      errors = rest
    }
  }

  // 输入时实时校验昵称（空格会破坏 @mention 匹配）
  function onNicknameInput(e) {
    const msg = validateNickname(e.target.value)
    if (msg) {
      errors = { ...errors, nickname: msg }
    } else if (errors.nickname) {
      const { nickname: _omit, ...rest } = errors
      errors = rest
    }
  }

  function validate() {
    const e = {}
    const agentIdErr = validateAgentId()
    if (agentIdErr) e.agentId = agentIdErr
    const nickErr = validateNickname()
    if (nickErr) e.nickname = nickErr
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
      agent_id: agentId.trim() || undefined,
      nickname: nickname.trim(),
      model_id: modelId.trim(),
      tool_ids: selectedToolIds,
      template_id: (templateId || '').trim() || null,
      template_arguments: parsedArgs,
      system_prompt: systemPrompt.trim(),
      myself_view: myselfView.trim(),
      description: description.trim(),
      labels: parseLabels(labelsText),
      avatar: avatar.trim(),
    }
    try {
      if (isEdit) await agents.update(originalAgentId, payload)
      else await agents.create(payload)
      await refreshAgents()
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="agent-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <div class="form-header">
    <h3>{isEdit ? t('editAgent') : t('createAgent')}</h3>
    <button type="button" class="btn btn-back" onclick={onCancel} disabled={submitting} title={t('cancel')}>&larr; {t('cancel')}</button>
  </div>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="form-group">
    <label for="agent_id">{t('agentId')}</label>
    <input id="agent_id" type="text" bind:value={agentId} placeholder={t('agentIdPlaceholder')} oninput={onAgentIdInput} aria-invalid={errors.agentId ? 'true' : undefined} />
    {#if errors.agentId}<span class="field-error">{errors.agentId}</span>{/if}
    <span class="field-hint">{t('agentIdHint')}</span>
  </div>

  <div class="form-group">
    <label for="agent_nickname">{t('agentNickname')} <span class="required">{t('required')}</span></label>
    <input id="agent_nickname" type="text" bind:value={nickname} placeholder={t('agentNicknamePlaceholder')} oninput={onNicknameInput} />
    {#if errors.nickname}<span class="field-error">{errors.nickname}</span>{/if}
  </div>

  <div class="form-group">
    <label for="agent_model">{t('agentModelId')} <span class="required">{t('required')}</span></label>
    <div class="model-input-row">
      <input id="agent_model" type="text" bind:value={modelId} placeholder={t('agentModelSelectHint')} class="model-input" />
      <div class="model-picker">
        {#if modelsLoading}
          <span class="hint">{t('loading')}</span>
        {:else}
          <select value={modelPickerValue} onchange={onModelPickerChange}>
            <option value="">{t('selectModelPlaceholder')}</option>
            {#each groupedModels.untagged as m (m.model_id)}
              <option value={m.model_id}>{m.model_id} [{m.model_name}]</option>
            {/each}
            {#each groupedModels.tagged as [tag, models] (tag)}
              <optgroup label={tag}>
                {#each models as m (m.model_id)}
                  <option value={m.model_id}>{m.model_id} [{m.model_name}]</option>
                {/each}
              </optgroup>
            {/each}
          </select>
        {/if}
      </div>
    </div>
    {#if errors.modelId}<span class="field-error">{errors.modelId}</span>{/if}
  </div>

  <div class="form-group">
    <label>{t('tools')}</label>
    <ToolSelector bind:selectedToolIds />
  </div>

  <div class="form-group">
    <label>{t('agentTemplateId')}</label>
    <div class="template-selector-wrapper">
      <PromptTemplateSelector bind:selectedTemplateId={templateId} />
    </div>
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
    <label for="agent_desc">{t('agentDescription')}</label>
    <input id="agent_desc" type="text" bind:value={description} placeholder={t('agentDescriptionPlaceholder')} />
  </div>

  <div class="form-group">
    <label for="agent_myself">{t('agentMyselfView')}</label>
    <textarea id="agent_myself" bind:value={myselfView} rows="3" placeholder={t('agentMyselfViewPlaceholder')}></textarea>
  </div>

  <div class="form-group">
    <label for="agent_labels">{t('agentLabels')}</label>
    <input id="agent_labels" type="text" bind:value={labelsText} placeholder={t('agentLabelsPlaceholder')} />
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
  .form-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .form-header h3 { margin: 0; color: var(--text); }
  .btn-back { background: transparent; color: var(--text-secondary); border: 1px solid var(--border); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; transition: all 0.15s; }
  .btn-back:hover:not(:disabled) { background: var(--border); color: var(--text); }
  .btn-back:disabled { opacity: 0.5; cursor: not-allowed; }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, textarea, select { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  textarea { resize: vertical; }
  .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
  .field-hint { color: var(--text-secondary); font-size: 0.78rem; margin-top: 2px; font-style: italic; }
  .hint { font-size: 0.85rem; color: var(--text-secondary); }
  .model-input-row { display: flex; gap: 8px; align-items: center; }
  .model-input { flex: 1; }
  .model-picker { flex-shrink: 0; }
  .model-picker select { min-width: 160px; }
  .template-selector-wrapper {
    border: 1px solid var(--border);
    border-radius: 6px;
    max-height: 220px;
    overflow-y: auto;
    background: var(--bg-secondary);
  }
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

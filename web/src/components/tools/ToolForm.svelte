<script>
  import { tools, mcpServers } from '../../lib/api.js'
  import { t } from '../../lib/i18n.svelte.js'
  import JsonEditor from '../JsonEditor.svelte'

  let { tool = null, mcpServer = null, onSuccess, onCancel } = $props()

  const _init = tool ?? {}
  const isEdit = tool !== null
  const isMcpEdit = mcpServer !== null

  let tool_type = $state(isMcpEdit ? 'mcp' : (_init.tool_type ?? 'function'))
  let name = $state(_init.name ?? '')
  let description = $state(_init.description ?? '')
  let parameters_text = $state(
    _init.parameters ? JSON.stringify(_init.parameters, null, 2) : ''
  )
  let function_file_path = $state(_init.function_file_path ?? '')
  let function_name = $state(_init.function_name ?? '')
  let skill_dir = $state(_init.skill_dir ?? '')

  const MCP_PLACEHOLDER = JSON.stringify({
    mcpServers: {
      time: { command: 'uvx', args: ['mcp-server-time', '--local-timezone=Asia/Shanghai'] },
      fetch: { command: 'uvx', args: ['mcp-server-fetch'] },
    }
  }, null, 2)

  let mcp_config_text = $state(isMcpEdit ? JSON.stringify(mcpServer.config, null, 2) : '')

  let errors = $state({})
  let submitError = $state('')
  let submitting = $state(false)

  function validate() {
    const e = {}
    if (tool_type === 'mcp') {
      if (!mcp_config_text.trim()) {
        e.mcp_config = t('mcpConfigRequired')
      } else {
        try {
          const parsed = JSON.parse(mcp_config_text)
          if (!parsed.mcpServers || typeof parsed.mcpServers !== 'object') {
            e.mcp_config = t('mcpServersMissing')
          }
        } catch {
          e.mcp_config = t('jsonInvalid')
        }
      }
    } else if (tool_type === 'skill') {
      if (!skill_dir.trim()) e.skill_dir = t('skillDirRequired')
    } else {
      if (!name.trim()) e.name = t('toolNameRequired')
      if (!description.trim()) e.description = t('toolDescRequired')
      if (!parameters_text.trim()) {
        e.parameters = t('parametersRequired')
      } else {
        try { JSON.parse(parameters_text) } catch { e.parameters = t('jsonInvalid') }
      }
      if (!function_file_path.trim()) e.function_file_path = t('functionFileRequired')
      if (!function_name.trim()) e.function_name = t('functionNameRequired')
    }
    errors = e
    return Object.keys(e).length === 0
  }

  async function handleSubmit() {
    if (!validate()) return
    submitting = true
    submitError = ''
    try {
      if (tool_type === 'mcp') {
        const config = JSON.parse(mcp_config_text)
        if (isMcpEdit) await mcpServers.delete(mcpServer.serverName)
        await tools.createMcp(config)
      } else if (tool_type === 'skill') {
        await tools.createSkill(skill_dir.trim())
      } else {
        const config = {
          tool_type,
          name: name.trim(),
          description: description.trim(),
          parameters: JSON.parse(parameters_text),
        }
        if (function_file_path.trim()) config.function_file_path = function_file_path.trim()
        if (function_name.trim()) config.function_name = function_name.trim()
        if (isEdit) await tools.update(_init.tool_id, config)
        else await tools.create(config)
      }
      onSuccess()
    } catch (err) {
      submitError = err.message || t('operationFailed')
    } finally {
      submitting = false
    }
  }
</script>

<form class="tool-form" onsubmit={(e) => { e.preventDefault(); handleSubmit() }}>
  <h3>{isMcpEdit ? t('editMcpServer') : (isEdit ? t('editTool') : t('registerTool'))}</h3>

  {#if submitError}
    <div class="form-error">{submitError}</div>
  {/if}

  <div class="form-group">
    <span class="radio-label">{t('toolTypeLabel')} <span class="required">{t('required')}</span></span>
    <div class="radio-group">
      {#each [['function','Function'],['mcp','MCP'],['skill','Skill']] as [val, label]}
        <label class="radio-item" class:disabled={isEdit || isMcpEdit}>
          <input type="radio" name="tool_type" value={val} bind:group={tool_type} disabled={isEdit || isMcpEdit} />
          {label}
        </label>
      {/each}
    </div>
  </div>

  {#if tool_type === 'mcp'}
    <div class="form-group">
      <label for="mcp_config">{t('mcpServerConfig')} <span class="required">{t('required')}</span></label>
      <JsonEditor id="mcp_config" bind:value={mcp_config_text} rows={12} placeholder={MCP_PLACEHOLDER} />
      {#if errors.mcp_config}<span class="field-error">{errors.mcp_config}</span>{/if}
      <span class="hint">{t('mcpConfigHint')}</span>
    </div>
  {:else if tool_type === 'skill'}
    <div class="form-group">
      <label for="skill_dir">{t('skillDirLabel')} <span class="required">{t('required')}</span></label>
      <input id="skill_dir" type="text" bind:value={skill_dir} placeholder={t('skillDirPlaceholder')} />
      {#if errors.skill_dir}<span class="field-error">{errors.skill_dir}</span>{/if}
      <span class="hint">{t('skillDirHint')}</span>
    </div>
  {:else}
    <div class="form-group">
      <label for="name">{t('name')} <span class="required">{t('required')}</span></label>
      <input id="name" type="text" bind:value={name} placeholder={t('toolNamePlaceholder')} />
      {#if errors.name}<span class="field-error">{errors.name}</span>{/if}
    </div>

    <div class="form-group">
      <label for="description">{t('description')} <span class="required">{t('required')}</span></label>
      <textarea id="description" bind:value={description} rows="2" placeholder={t('toolDescPlaceholder')}></textarea>
      {#if errors.description}<span class="field-error">{errors.description}</span>{/if}
    </div>

    <div class="form-group">
      <label for="parameters">Parameters (JSON Schema) <span class="required">{t('required')}</span></label>
      <JsonEditor id="parameters" bind:value={parameters_text} rows={6} placeholder={t('parametersPlaceholder')} />
      {#if errors.parameters}<span class="field-error">{errors.parameters}</span>{/if}
    </div>

    <div class="form-group">
      <label for="function_file_path">{t('functionFilePath')} <span class="required">{t('required')}</span></label>
      <input id="function_file_path" type="text" bind:value={function_file_path} placeholder={t('functionFilePathPlaceholder')} />
      {#if errors.function_file_path}<span class="field-error">{errors.function_file_path}</span>{/if}
      <span class="hint">{t('functionFilePathHint')}</span>
    </div>

    <div class="form-group">
      <label for="function_name">{t('functionName')} <span class="required">{t('required')}</span></label>
      <input id="function_name" type="text" bind:value={function_name} placeholder={t('functionNamePlaceholder')} />
      {#if errors.function_name}<span class="field-error">{errors.function_name}</span>{/if}
      <span class="hint">{t('functionNameHint')}</span>
    </div>
  {/if}

  <div class="form-actions">
    <button type="button" class="btn btn-cancel" onclick={onCancel} disabled={submitting}>{t('cancel')}</button>
    <button type="submit" class="btn btn-primary" disabled={submitting}>
      {submitting ? t('submitting') : (isEdit || isMcpEdit ? t('save') : t('register'))}
    </button>
  </div>
</form>

<style>
  .tool-form { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  h3 { margin: 0 0 16px 0; color: var(--text); }
  .form-error { background: var(--danger); color: #fff; padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 0.9rem; }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; }
  label { margin-bottom: 4px; font-size: 0.9rem; color: var(--text-secondary); }
  .required { color: var(--danger); }
  input, select, textarea { padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-secondary); color: var(--text); font-size: 0.9rem; font-family: inherit; }
  input:disabled { opacity: 0.6; cursor: not-allowed; }
  .radio-label { font-size: 0.9rem; color: var(--text-secondary); margin-bottom: 6px; }
  .radio-group { display: flex; gap: 20px; flex-wrap: wrap; }
  .radio-item { display: flex; align-items: center; gap: 6px; font-size: 0.9rem; color: var(--text); cursor: pointer; }
  .radio-item.disabled { opacity: 0.6; cursor: not-allowed; }
  .radio-item input[type="radio"] { padding: 0; border: none; background: none; width: auto; cursor: pointer; }
  textarea { resize: vertical; font-family: monospace; }
  .field-error { color: var(--danger); font-size: 0.8rem; margin-top: 2px; }
  .hint { color: var(--text-secondary); font-size: 0.8rem; margin-top: 4px; }
  .form-actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 16px; }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .btn-cancel { background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
</style>

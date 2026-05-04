<script>
  import { agents } from '../../lib/api.js'
  import AgentForm from './AgentForm.svelte'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let agentList = $state([])
  let loading = $state(true)
  let error = $state('')
  let showForm = $state(false)
  let editingAgent = $state(null)
  let deleteTarget = $state(null)

  async function fetchAgents() {
    loading = true
    error = ''
    try {
      const data = await agents.list()
      agentList = data.agents ?? []
    } catch (err) {
      error = err.message || t('fetchAgentsFailed')
    } finally {
      loading = false
    }
  }

  function handleCreate() { editingAgent = null; showForm = true }
  function handleEdit(agent) { editingAgent = agent; showForm = true }
  function handleFormSuccess() { showForm = false; editingAgent = null; fetchAgents() }
  function handleFormCancel() { showForm = false; editingAgent = null }
  function handleDeleteClick(agent) { deleteTarget = agent }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const id = deleteTarget.agent_id
    deleteTarget = null
    try {
      await agents.delete(id)
      fetchAgents()
    } catch (err) {
      error = err.message || t('deleteAgentFailed')
    }
  }

  function handleDeleteCancel() { deleteTarget = null }

  $effect(() => { fetchAgents() })
</script>

<div class="agents-page">
  <div class="page-header">
    <h2>{t('agentsPageTitle')}</h2>
    {#if !showForm}
      <button class="btn btn-primary" onclick={handleCreate}>{t('createAgent')}</button>
    {/if}
  </div>

  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if showForm}
      <AgentForm agent={editingAgent} onSuccess={handleFormSuccess} onCancel={handleFormCancel} />
    {/if}

    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else if agentList.length === 0 && !error}
      <div class="empty">{t('noAgents')}</div>
    {:else if agentList.length > 0}
      <div class="agent-list">
        {#each agentList as agent (agent.agent_id)}
          <div class="agent-card">
            <div class="agent-info">
              <div class="agent-name-row">
                <span class="agent-name">{agent.nickname}</span>
                <span class="agent-meta">{agent.model_id} · {agent.tool_ids?.length || 0} tools{agent.template_id ? ' · ' + agent.template_id : ''}</span>
              </div>
              {#if agent.myself_view}
                <div class="agent-myself">{agent.myself_view}</div>
              {/if}
              {#if agent.description}
                <div class="agent-description">{agent.description}</div>
              {/if}
              <div class="agent-timestamp">{agent.last_modified ?? ''}</div>
            </div>
            <div class="agent-actions">
              <button class="btn btn-sm" onclick={() => handleEdit(agent)}>{t('edit')}</button>
              <button class="btn btn-sm btn-danger" onclick={() => handleDeleteClick(agent)}>{t('delete')}</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteAgent', { id: deleteTarget?.nickname ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .agents-page {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  h2 { margin: 0; color: var(--text); }
  .btn { padding: 8px 18px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-sm { padding: 4px 12px; font-size: 0.85rem; background: var(--bg-secondary); color: var(--text); border: 1px solid var(--border); border-radius: 4px; }
  .btn-sm:hover { opacity: 0.8; }
  .btn-danger { background: var(--danger); color: #fff; border: none; }
  .btn-danger:hover { background: var(--danger-hover); }
  .error-msg { background: var(--danger); color: #fff; padding: 10px 14px; border-radius: 6px; margin: 0 20px; font-size: 0.9rem; flex-shrink: 0; }
  .page-content {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
    padding: 20px;
  }
  .loading, .empty { text-align: center; padding: 40px 0; color: var(--text-secondary); font-size: 1rem; }
  .agent-list { display: flex; flex-direction: column; gap: 12px; }
  .agent-card { display: flex; justify-content: space-between; align-items: flex-start; padding: 14px 16px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; }
  .agent-info { flex: 1; min-width: 0; }
  .agent-name-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; gap: 8px; }
  .agent-name { font-weight: 600; color: var(--text); font-size: 0.95rem; }
  .agent-meta { font-size: 0.8rem; color: var(--text-secondary); white-space: nowrap; }
  .agent-myself { color: var(--primary); font-size: 0.85rem; margin-bottom: 2px; }
  .agent-description { color: var(--text-secondary); font-size: 0.82rem; white-space: pre-wrap; word-break: break-word; margin-bottom: 4px; }
  .agent-timestamp { font-size: 0.75rem; color: var(--text-secondary); opacity: 0.7; }
  .agent-actions { display: flex; align-items: center; gap: 8px; margin-left: 16px; flex-shrink: 0; }

  .page-content::-webkit-scrollbar {
    width: 8px;
  }
  .page-content::-webkit-scrollbar-track {
    background: transparent;
  }
  .page-content::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .page-content:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .page-content:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }
</style>

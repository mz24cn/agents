<script>
  import { env } from '../../lib/api.js'
  import ConfirmDialog from '../ConfirmDialog.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let {
    triggerDetect = 0,
    showAddForm = false,
  } = $props()

  let envVars = $state([])       // [{ key, value }]
  let loading = $state(true)
  let error = $state('')

  // --- 新增表单 ---
  let newKey = $state('')
  let newValue = $state('')
  let saving = $state(false)
  let saveError = $state('')

  // --- 删除确认 ---
  let deleteTarget = $state(null)

  // --- 检测结果 ---
  let detecting = $state(false)
  let detectError = $state('')
  let unsetKeys = $state([])     // 检测到但尚未配置的 key 列表

  // ============================
  // 加载
  // ============================
  async function fetchEnvVars() {
    loading = true
    error = ''
    try {
      const data = await env.list()
      const map = data.env ?? data
      envVars = Object.entries(map).map(([key, value]) => ({ key, value }))
    } catch (err) {
      error = err.message || t('fetchEnvFailed')
    } finally {
      loading = false
    }
  }

  // ============================
  // 新增
  // ============================
  async function handleAdd() {
    if (!newKey.trim()) {
      saveError = t('envKeyRequired')
      return
    }
    saveError = ''
    saving = true
    try {
      await env.set(newKey.trim(), newValue)
      newKey = ''
      newValue = ''
      await fetchEnvVars()
    } catch (err) {
      saveError = err.message || t('saveEnvFailed')
    } finally {
      saving = false
    }
  }

  // ============================
  // 删除
  // ============================
  function handleDeleteClick(item) {
    deleteTarget = item
  }

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    const key = deleteTarget.key
    deleteTarget = null
    try {
      await env.delete(key)
      await fetchEnvVars()
    } catch (err) {
      error = err.message || t('deleteEnvFailed')
    }
  }

  function handleDeleteCancel() {
    deleteTarget = null
  }

  // ============================
  // 检测
  // ============================
  async function handleDetect() {
    detecting = true
    detectError = ''
    unsetKeys = []
    try {
      const data = await env.detect()
      const detected = data.detected_keys ?? data.keys ?? []
      const existing = new Set(envVars.map((v) => v.key))
      unsetKeys = detected.filter((k) => !existing.has(k))
    } catch (err) {
      detectError = err.message || t('detectEnvFailed')
    } finally {
      detecting = false
    }
  }

  // 点击"检测"按钮时填充新表单
  function fillKey(key) {
    newKey = key
    newValue = ''
    showAddForm = true
  }

  // ============================
  // 响应外部触发
  // ============================
  $effect(() => {
    // triggerDetect 变化 → 执行检测
    if (triggerDetect > 0) {
      handleDetect()
    }
  })

  $effect(() => {
    // showAddForm 变化时重置表单状态（由 SetupPage 驱动）
  })

  // 初始加载
  $effect(() => {
    fetchEnvVars()
  })
</script>

<div class="env-page">
  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  <div class="page-content">
    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else}
      <!-- ============ 检测结果（顶部） ============ -->
      {#if detecting}
        <div class="detect-section">
          <div class="loading">{t('detectEnvVars')}...</div>
        </div>
      {:else if detectError}
        <div class="detect-section">
          <div class="error-msg">{detectError}</div>
        </div>
      {:else if unsetKeys.length > 0}
        <div class="detect-section">
          <div class="detect-header">{t('detectedUnsetKeys')}</div>
          <div class="detect-list">
            {#each unsetKeys as key}
              <button
                class="detect-chip"
                onclick={() => fillKey(key)}
                title={t('addEnvVar')}
              >
                {key}
              </button>
            {/each}
          </div>
        </div>
      {:else if triggerDetect > 0}
        <div class="detect-section">
          <div class="detect-ok">{t('noUnsetKeys')}</div>
        </div>
      {/if}

      <!-- ============ 新增表单（顶部） ============ -->
      {#if showAddForm}
        <div class="add-form">
          <div class="form-row">
            <input
              type="text"
              placeholder={t('envKey')}
              bind:value={newKey}
              class="form-input"
            />
            <input
              type="text"
              placeholder={t('envValue')}
              bind:value={newValue}
              class="form-input"
            />
            <button
              class="btn btn-sm btn-primary"
              onclick={handleAdd}
              disabled={saving}
            >
              {saving ? '...' : t('addEnvVar')}
            </button>
          </div>
          {#if saveError}
            <div class="error-msg">{saveError}</div>
          {/if}
        </div>
      {/if}

      <!-- ============ 变量列表（底部） ============ -->
      {#if envVars.length === 0 && !showAddForm}
        <div class="empty">{t('noEnvVars')}</div>
      {:else}
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t('envKey')}</th>
                <th>{t('envValue')}</th>
                <th>{t('actions')}</th>
              </tr>
            </thead>
            <tbody>
              {#each envVars as item (item.key)}
                <tr>
                  <td class="key-cell">{item.key}</td>
                  <td class="value-cell">{item.value}</td>
                  <td class="actions">
                    <button
                      class="btn btn-sm btn-danger"
                      onclick={() => handleDeleteClick(item)}
                    >{t('delete')}</button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  </div>
</div>

<ConfirmDialog
  open={deleteTarget !== null}
  title={t('confirmDeleteEnv', { key: deleteTarget?.key ?? '' })}
  onConfirm={handleDeleteConfirm}
  onCancel={handleDeleteCancel}
/>

<style>
  .env-page {
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .page-content {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
  }
  .loading, .empty {
    text-align: center;
    padding: 40px 0;
    color: var(--text-secondary);
    font-size: 1rem;
  }
  .error-msg {
    background: var(--danger);
    color: #fff;
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 0.9rem;
    margin-bottom: 12px;
  }

  /* ---- 表格 ---- */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
  }
  th {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-weight: 600;
    font-size: 0.85rem;
  }
  td { font-size: 0.9rem; color: var(--text); }
  .key-cell {
    font-family: monospace;
    font-weight: 600;
    white-space: nowrap;
  }
  .value-cell {
    word-break: break-all;
    max-width: 480px;
  }
  .actions { display: flex; gap: 8px; }

  /* ---- 新增表单 ---- */
  .add-form {
    margin-bottom: 16px;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
  }
  .form-row {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .form-input {
    flex: 1;
    min-width: 140px;
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--text);
    font-size: 0.85rem;
    outline: none;
  }
  .form-input:focus {
    border-color: var(--primary);
  }
  /* btn-sm 与 ModelsPage 完全一致 */
  .btn-sm {
    padding: 4px 12px;
    font-size: 0.85rem;
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
  }
  .btn-sm:hover { opacity: 0.8; }
  .btn-primary {
    background: var(--primary);
    color: #fff;
    border: 1px solid var(--primary);
  }
  .btn-primary:hover { opacity: 0.85; }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-danger {
    background: var(--danger);
    color: #fff;
    border: none;
  }
  .btn-danger:hover { background: var(--danger-hover); }

  /* ---- 检测结果 ---- */
  .detect-section {
    margin-bottom: 16px;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
  }
  .detect-header {
    font-weight: 600;
    font-size: 0.9rem;
    color: var(--text);
    margin-bottom: 10px;
  }
  .detect-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .detect-chip {
    padding: 6px 14px;
    border: 1px solid var(--primary);
    border-radius: 20px;
    background: transparent;
    color: var(--primary);
    font-family: monospace;
    font-size: 0.85rem;
    cursor: pointer;
    transition: all 0.15s;
  }
  .detect-chip:hover {
    background: var(--primary);
    color: #fff;
  }
  .detect-ok {
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  /* ---- 滚动条样式：默认隐藏，悬停时显示 ---- */
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

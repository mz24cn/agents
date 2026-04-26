<script>
  import { promptTemplates } from '../../lib/api.js'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  /**
   * 面板右侧：模板列表
   * selectedTemplateId: 当前选中的模板 ID（持久保留）
   * onSelect(result): result = null | { type: 'direct', content } | { type: 'template', template }
   */
  let { selectedTemplateId = $bindable(null), onSelect } = $props()

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

  function handleSelect(tpl) {
    selectedTemplateId = tpl.template_id
    const placeholders = extractPlaceholders(tpl.content)
    if (placeholders.length === 0) {
      onSelect?.({ type: 'direct', content: tpl.content, template: tpl })
    } else {
      onSelect?.({ type: 'template', template: tpl })
    }
  }

  $effect(() => {
    fetchTemplates()
  })

  // 当模板列表加载完成后，如果有已选中的模板，自动触发选中
  $effect(() => {
    if (!loading && selectedTemplateId && templateList.length > 0) {
      const tpl = templateList.find(t => t.template_id === selectedTemplateId)
      if (tpl) {
        const placeholders = extractPlaceholders(tpl.content)
        if (placeholders.length === 0) {
          onSelect?.({ type: 'direct', content: tpl.content, template: tpl })
        } else {
          onSelect?.({ type: 'template', template: tpl })
        }
      }
    }
  })
</script>

<div class="template-list">
  {#if loading}
    <div class="hint">{t('loading')}</div>
  {:else if error}
    <div class="hint error">{error}</div>
  {:else if templateList.length === 0}
    <div class="hint">{t('noTemplates')}</div>
  {:else}
    {#each templateList as tpl (tpl.template_id)}
      <button
        class="template-item"
        class:selected={selectedTemplateId === tpl.template_id}
        onclick={() => handleSelect(tpl)}
      >
        <span class="tpl-name">{tpl.template_id}</span>
      </button>
    {/each}
  {/if}
</div>

<style>
  .template-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 4px;
  }
  .hint {
    font-size: 0.85rem;
    color: var(--text-secondary);
    padding: 8px 4px;
  }
  .hint.error { color: var(--danger); }
  .template-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid transparent;
    background: none;
    color: var(--text);
    font-size: 0.85rem;
    cursor: pointer;
    transition: background-color 0.12s, border-color 0.12s;
    white-space: nowrap;
  }
  .template-item:hover {
    background: var(--bg-secondary);
    border-color: var(--border);
  }
  .template-item.selected {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  .tpl-name { font-weight: 500; }
</style>

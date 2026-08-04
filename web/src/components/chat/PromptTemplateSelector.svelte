<script>
  import { catalog, loadPromptTemplates } from '../../lib/catalog-state.svelte.js'
  import { extractPlaceholders } from '../../lib/placeholder.js'
  import { t } from '../../lib/i18n.svelte.js'

  /**
   * 面板右侧：模板列表
   * selectedTemplateId: 当前选中的模板 ID（持久保留）
   * onSelect(result): result = null | { type: 'direct', content } | { type: 'template', template }
   * 点击模板项为 toggle：再次点击已选中的模板会取消选中（selectedTemplateId 置为 null 并回调 onSelect(null)）
   */
  let { selectedTemplateId = $bindable(null), onSelect } = $props()

  let templateList = $derived(catalog.promptTemplates.items)
  let loading = $derived(catalog.promptTemplates.loading && !catalog.promptTemplates.loaded)
  let error = $derived(catalog.promptTemplates.error)

  // 按第一项标签分组
  let groupedTemplates = $derived.by(() => {
    const groups = new Map()
    const noTagItems = []
    
    for (const tpl of templateList) {
      const firstTag = (tpl.labels && tpl.labels.length > 0) ? tpl.labels[0] : ''
      if (!firstTag) {
        noTagItems.push(tpl)
      } else {
        if (!groups.has(firstTag)) groups.set(firstTag, [])
        groups.get(firstTag).push(tpl)
      }
    }
    
    // 按标签名排序，空标签放在最后
    const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    
    return { tagged: sortedGroups, untagged: noTagItems }
  })

  function handleSelect(tpl) {
    // toggle：已选中则取消选中，未选中则选中
    if (selectedTemplateId === tpl.template_id) {
      selectedTemplateId = null
      onSelect?.(null)
      return
    }
    selectedTemplateId = tpl.template_id
    const placeholders = extractPlaceholders(tpl.content)
    if (placeholders.length === 0) {
      onSelect?.({ type: 'direct', content: tpl.content, template: tpl })
    } else {
      onSelect?.({ type: 'template', template: tpl })
    }
  }

  $effect(() => { loadPromptTemplates().catch(() => {}) })

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
    <!-- 无标签的模板（默认组） -->
    {#each groupedTemplates.untagged as tpl (tpl.template_id)}
      <button
        type="button"
        class="template-item"
        class:selected={selectedTemplateId === tpl.template_id}
        onclick={() => handleSelect(tpl)}
      >
        <span class="tpl-name">{tpl.template_id}</span>
      </button>
    {/each}
    
    <!-- 有标签的模板分组 -->
    {#each groupedTemplates.tagged as [tag, templates] (tag)}
      <div class="template-group">
        <div class="group-header">
          <span class="group-label">{tag}</span>
        </div>
        {#each templates as tpl (tpl.template_id)}
          <button
            type="button"
            class="template-item"
            class:selected={selectedTemplateId === tpl.template_id}
            onclick={() => handleSelect(tpl)}
          >
            <span class="tpl-name">{tpl.template_id}</span>
          </button>
        {/each}
      </div>
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
  .template-group {
    margin-bottom: 4px;
  }
  .group-header {
    padding: 8px 10px 4px 10px;
  }
  .group-label {
    font-size: 0.72rem;
    font-weight: 600;
    color: #10b981;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
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

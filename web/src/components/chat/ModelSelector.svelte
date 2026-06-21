<script>
  import { catalog, loadModels } from '../../lib/catalog-state.svelte.js'
  import { t } from '../../lib/i18n.svelte.js'

  let { selectedModelId = $bindable(''), onchange, disabled = false } = $props()

  let modelList = $derived(catalog.models.items)
  let loading = $derived(catalog.models.loading && !catalog.models.loaded)
  let error = $derived(catalog.models.error)

  // 按第一项标签分组
  let groupedModels = $derived.by(() => {
    const groups = new Map()
    const noTagItems = []
    
    for (const model of modelList) {
      const firstTag = (model.labels && model.labels.length > 0) ? model.labels[0] : ''
      if (!firstTag) {
        noTagItems.push(model)
      } else {
        if (!groups.has(firstTag)) groups.set(firstTag, [])
        groups.get(firstTag).push(model)
      }
    }
    
    // 按标签名排序，空标签放在最后
    const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    
    return { tagged: sortedGroups, untagged: noTagItems }
  })

  function handleChange(e) {
    selectedModelId = e.target.value
    onchange?.(selectedModelId)
  }

  $effect(() => { loadModels().catch(() => {}) })

  // 如果已保存/恢复的模型 ID 不在共享列表中，则清空选择。
  $effect(() => {
    if (catalog.models.loaded && selectedModelId && !modelList.some(m => m.model_id === selectedModelId)) {
      selectedModelId = ''
      onchange?.(selectedModelId)
    }
  })
</script>

<div class="model-selector">
  {#if loading}
    <span class="hint">{t('loading')}</span>
  {:else if error}
    <span class="hint error">{error}</span>
  {:else}
    <select id="model-select" value={selectedModelId} onchange={handleChange} disabled={disabled}>
      <option value="">{t('selectModelPlaceholder')}</option>
      <!-- 无标签的模型 -->
      {#each groupedModels.untagged as m (m.model_id)}
        <option value={m.model_id}>{m.model_id} [{m.model_name}]</option>
      {/each}
      <!-- 有标签的模型分组 -->
      {#each groupedModels.tagged as [tag, models] (tag)}
        <optgroup label={tag}>
          {#each models as m (m.model_id)}
            <option value={m.model_id}>{m.model_id} [{m.model_name}]</option>
          {/each}
        </optgroup>
      {/each}
    </select>
    {#if !selectedModelId}
      <span class="hint">{t('selectModelHint')}</span>
    {/if}
  {/if}
</div>

<style>
  .model-selector { display: flex; align-items: center; gap: 8px; }
  select {
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 0.9rem;
    min-width: 180px;
  }
  select:disabled { opacity: 0.6; cursor: not-allowed; }
  :global(optgroup) { color: #10b981; font-weight: 600; font-size: 0.82rem; }
  :global(optgroup option) { color: var(--text); font-weight: normal; font-size: 0.9rem; }
  .hint { font-size: 0.8rem; color: var(--text-secondary); }
  .hint.error { color: var(--danger); }
</style>

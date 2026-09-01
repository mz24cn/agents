<script>
  // Svelte 5 no longer creates an implicit variable for a component to render
  // itself recursively. Import this file under a different name so expanding a
  // category can safely render its child categories in production builds.
  import CategoryNode from './SessionCategoryNode.svelte'

  let {
    node,
    depth = 0,
    expandedPaths,
    selectedCategory = '',
    onToggle,
    onSelect,
  } = $props()

  let path = $derived(node.category || '')
  let expanded = $derived(expandedPaths.has(path))
  let categoryChildren = $derived((node.children || []).filter(child => child && typeof child === 'object'))
  let terminal = $derived(Boolean(node.category) && categoryChildren.length === 0)

  function handleClick() {
    if (terminal) onSelect(path)
    else onToggle(path)
  }
</script>

<div class="category-node">
  <button
    class="category-row"
    class:selected={terminal && selectedCategory === path}
    data-category-path={path}
    style={`--category-depth: ${depth}`}
    onclick={handleClick}
    title={node.name}
  >
    <span class="category-arrow" class:leaf={terminal}>{terminal ? '·' : (expanded ? '▾' : '▸')}</span>
    <span class="category-name">{node.name}</span>
    <span class="category-count">{node.session_count ?? 0}</span>
  </button>
  {#if !terminal && expanded}
    <div class="category-children">
      {#each categoryChildren as child (`${path}/${child.id}`)}
        <CategoryNode
          node={{ ...child, category: `${path}/${child.id}` }}
          depth={depth + 1}
          {expandedPaths}
          {selectedCategory}
          {onToggle}
          {onSelect}
        />
      {/each}
    </div>
  {/if}
</div>

<style>
  .category-row {
    width: 100%;
    min-height: 30px;
    padding: 4px 8px 4px calc(8px + var(--category-depth) * 14px);
    border: none;
    background: transparent;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 0.8rem;
    text-align: left;
    cursor: pointer;
  }
  .category-row:hover { background: var(--border); color: var(--text); }
  .category-row.selected { color: var(--primary); font-weight: 600; }
  .category-arrow { width: 12px; flex-shrink: 0; text-align: center; }
  .category-arrow.leaf { opacity: 0.45; }
  .category-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .category-count { flex-shrink: 0; opacity: 0.5; font-size: 0.72rem; font-variant-numeric: tabular-nums; }
</style>
